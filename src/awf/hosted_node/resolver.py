"""Point-in-time protected mounts. Never wired into the current local worker."""

import asyncio
import threading
from collections.abc import Mapping
from concurrent.futures import TimeoutError as FutureTimeout
from types import MappingProxyType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.session import make_engine, make_session_factory
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.schemas import Binding, Identity
from awf.node.compose_manager import AuthMount
from awf.node.secret_mounts import LocalSecretLeaseResolution
from awf.profiles.models import WorkspaceProfile


class ProtectedResolver:
    """Caller owns close(). Each instance has one bounded serial DB bridge.

    Bindings come from a trusted controller, never profile refs or namespaces.
    No plaintext is returned. Authorization is rechecked on every invocation.
    """

    def __init__(
        self,
        database_url: str,
        config: HostedSettings,
        identity: Identity,
        bindings: Mapping[str, Binding],
        *,
        timeout: float = 5.0,
    ):
        if not 0 < timeout <= 30:
            raise MaterialDeniedError("INVALID_BRIDGE_TIMEOUT")
        self.bindings = MappingProxyType(dict(bindings))
        self.timeout = timeout
        self._closed = False
        self._gate = threading.Lock()
        self._resolve_gate = asyncio.Lock()
        self._loop = asyncio.new_event_loop()

        def run_bridge() -> None:
            try:
                self._loop.run_forever()
            finally:
                self._loop.close()

        self._thread = threading.Thread(
            target=run_bridge, daemon=True, name="awf-protected-resolver"
        )
        self._thread.start()

        engine: AsyncEngine | None = None
        store: MaterialStore | None = None

        async def initialize() -> Receiver:
            nonlocal engine, store
            engine = self._engine = make_engine(database_url)
            factory = make_session_factory(engine)
            store = MaterialStore(config.material_root)
            return Receiver(config, factory, identity, store)

        async def cleanup_initialization() -> None:
            try:
                try:
                    if store is not None:
                        store.close()
                finally:
                    if engine is not None:
                        await engine.dispose()
            finally:
                self._loop.call_soon(self._loop.stop)

        future = asyncio.run_coroutine_threadsafe(initialize(), self._loop)
        try:
            self._receiver = future.result(timeout)
        except BaseException:
            # Initialization is synchronous on the bridge. Queue cleanup behind it
            # so even resources created after a timeout are released. The bridge
            # owns loop closure once cleanup finishes, even after this join expires.
            asyncio.run_coroutine_threadsafe(cleanup_initialization(), self._loop)
            self._thread.join(timeout)
            raise MaterialDeniedError("RESOLVER_UNAVAILABLE", 503) from None

    async def _resolve(
        self, profile: WorkspaceProfile, workspace_id: str
    ) -> LocalSecretLeaseResolution:
        return await self.resolve_current(self._receiver, self.bindings, profile, workspace_id)

    @staticmethod
    async def resolve_current(
        receiver: Receiver,
        bindings: Mapping[str, Binding],
        profile: WorkspaceProfile,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        """Same protected resolution for an in-process, identity-bound consumer."""
        mounts = []
        selected = []
        seen = set()
        for declaration in profile.secrets:
            binding = bindings.get(declaration.name)
            if (
                binding is None
                or binding.workspace_id != workspace_id
                or declaration.target != binding.target
                or declaration.kind != "mount"
                or declaration.mode != "ro"
                or declaration.provider != "protected"
                or declaration.ref is not None
                or declaration.target in seen
            ):
                raise MaterialDeniedError("PROTECTED_DECLARATION_MISMATCH")
            seen.add(declaration.target)
            status = await receiver.status(binding)
            if status.status != "current" or status.receipt_id is None:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
            async with receiver.factory() as session:
                prep = await receiver.preparation(session, binding)
                assert prep is not None
                directory = prep.id
            mounts.append(
                AuthMount(
                    source=str(receiver.store.path(directory, status.receipt_id)),
                    target=binding.target,
                    mode="ro",
                )
            )
            selected.append((binding, status.receipt_id))
        # Even a profile without declarations cannot bypass owner liveness.
        await receiver.identity_status()
        # Owner liveness alone does not detect grant revocation or refresh
        # while mounts were being assembled (including failed file cleanup).
        for binding, receipt_id in selected:
            status = await receiver.status(binding)
            if status.status != "current" or status.receipt_id != receipt_id:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
        return LocalSecretLeaseResolution(
            mounts=tuple(mounts), metadata={"protected_mount_count": len(mounts)}
        )

    def resolve(
        self, profile: WorkspaceProfile, *, workspace_id: str
    ) -> LocalSecretLeaseResolution:
        if not self._gate.acquire(timeout=self.timeout):
            raise MaterialDeniedError("RESOLVER_BUSY", 503)
        try:
            if self._closed:
                raise MaterialDeniedError("RESOLVER_CLOSED", 503)

            async def resolve_serially() -> LocalSecretLeaseResolution:
                # Future cancellation can precede coroutine cleanup. Keep the
                # bridge serialized until the previous resolution actually exits.
                async with self._resolve_gate:
                    return await self._resolve(profile, workspace_id)

            future = asyncio.run_coroutine_threadsafe(resolve_serially(), self._loop)
            try:
                return future.result(self.timeout)
            except (FutureTimeout, SQLAlchemyError, OSError):
                future.cancel()
                raise MaterialDeniedError("RESOLVER_UNAVAILABLE", 503) from None
        finally:
            self._gate.release()

    def close(self) -> None:
        with self._gate:
            if self._closed:
                return
            self._closed = True

            async def dispose() -> None:
                try:
                    async with self._resolve_gate:
                        self._receiver.closed = True
                        self._receiver.store.close()
                        await self._engine.dispose()
                finally:
                    # The bridge owns shutdown even if close() times out while
                    # waiting for resolution cancellation or disposal to finish.
                    self._loop.call_soon(self._loop.stop)

            future = asyncio.run_coroutine_threadsafe(dispose(), self._loop)
            try:
                future.result(self.timeout)
            finally:
                self._thread.join(self.timeout)
