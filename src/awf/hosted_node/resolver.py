"""Point-in-time protected mounts. Never wired into the current local worker."""

import asyncio
import threading
from collections.abc import Mapping
from concurrent.futures import TimeoutError as FutureTimeout
from types import MappingProxyType

from sqlalchemy.exc import SQLAlchemyError

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
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="awf-protected-resolver"
        )
        self._thread.start()

        async def initialize() -> Receiver:
            self._engine = make_engine(database_url)
            return Receiver(
                config,
                make_session_factory(self._engine),
                identity,
                MaterialStore(config.material_root),
            )

        future = asyncio.run_coroutine_threadsafe(initialize(), self._loop)
        try:
            self._receiver = future.result(timeout)
        except BaseException:
            future.cancel()
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout)
            self._loop.close()
            raise MaterialDeniedError("RESOLVER_UNAVAILABLE", 503) from None

    async def _resolve(
        self, profile: WorkspaceProfile, workspace_id: str
    ) -> LocalSecretLeaseResolution:
        mounts = []
        seen = set()
        for declaration in profile.secrets:
            binding = self.bindings.get(declaration.name)
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
            status = await self._receiver.status(binding)
            if status.status != "current" or status.receipt_id is None:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
            async with self._receiver.factory() as session:
                prep = await self._receiver.preparation(session, binding)
                assert prep is not None
                directory = prep.id
            mounts.append(
                AuthMount(
                    source=str(self._receiver.store.path(directory, status.receipt_id)),
                    target=binding.target,
                    mode="ro",
                )
            )
        # Even a profile without declarations cannot bypass owner liveness.
        await self._receiver.identity_status()
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
            future = asyncio.run_coroutine_threadsafe(
                self._resolve(profile, workspace_id), self._loop
            )
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
                self._receiver.closed = True
                self._receiver.store.close()
                await self._engine.dispose()

            future = asyncio.run_coroutine_threadsafe(dispose(), self._loop)
            try:
                future.result(self.timeout)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(self.timeout)
                self._loop.close()
