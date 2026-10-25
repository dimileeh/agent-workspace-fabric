"""Bind normal workspace consumers to current protected material authority."""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import or_, select

from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import ResourceReservationRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.models import Grant, Receipt
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.resolver import ProtectedResolver
from awf.hosted_node.runtime_schemas import POLICY_KEY, HostedWorkspace
from awf.hosted_node.runtime_views import PublishedView, RuntimeViews, owned_publication
from awf.hosted_node.schemas import Binding
from awf.node.compose_manager import AuthMount
from awf.node.secret_mounts import LocalSecretLeaseResolution
from awf.profiles.compose import AGENT_AUTH_ENV_VARS
from awf.profiles.models import WorkspaceProfile

if TYPE_CHECKING:
    from awf.api.schemas import WorkspaceCreateRequest

SESSION_KEY = "awf_hosted_runtime"
AGENT_TARGETS = {
    "codex": ("/run/awf/secrets/openai.api-key", "CODEX_API_KEY"),
    "claude_code": ("/run/awf/secrets/anthropic.api-key", "ANTHROPIC_API_KEY"),
}
_HISTORICAL_STATUSES = tuple(
    status.value for status in WorkspaceStatus if WorkspaceStateMachine.is_callback_terminal(status)
)


def _stored_association(value: object) -> HostedWorkspace:
    try:
        return HostedWorkspace.model_validate(value)
    except ValidationError:
        raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403) from None


@dataclass(frozen=True)
class AuthorizedMaterials:
    environment: dict[str, str] = field(repr=False)
    expires_at: datetime


class _SnapshotUnavailableError(MaterialDeniedError):
    """An exact material read failed; only proven grant advancement may restart it."""

    def __init__(self) -> None:
        super().__init__("PROTECTED_MATERIAL_UNAVAILABLE", 503)


class WorkspaceAuthority:
    def __init__(self, receiver: Receiver):
        self.receiver = receiver
        self.views = receiver.views

    async def admission(self) -> None:
        if not self.receiver.config.runtime_enabled or self.receiver.lifecycle is None:
            raise MaterialDeniedError("HOSTED_RUNTIME_DISABLED", 403)
        status = await self.receiver.identity_status()
        if not status.acknowledged:
            raise MaterialDeniedError("IDENTITY_ACKNOWLEDGEMENT_REQUIRED", 403)
        async with self.receiver.factory.begin() as session:
            row = await locked(session, self.receiver.identity)
            if row.phase != "ready" or row.recovery_key is None:
                raise MaterialDeniedError("NODE_ADMISSION_CLOSED", 409)

    async def admit(self, payload: "WorkspaceCreateRequest") -> AuthorizedMaterials:
        if payload.hosted is None:
            raise MaterialDeniedError("HOSTED_WORKSPACE_REQUIRED", 422)
        if payload.preflight.provider_readiness_override or payload.task.provider_recovery:
            raise MaterialDeniedError("HOSTED_CREDENTIAL_OVERRIDE_FORBIDDEN", 422)
        return await self.materials(payload.hosted, payload.task.agent.value, current=True)

    async def resource_limits(self, workspace_id: str) -> tuple[float, float]:
        await self.workspace(workspace_id)
        async with self.receiver.factory() as session:
            reservation = await ResourceReservationRepository(session).active_for_workspace(
                workspace_id
            )
            if reservation is None:
                raise MaterialDeniedError("HOSTED_WORKSPACE_RESERVATION_REQUIRED", 503)
            return reservation.peak_cpu, reservation.peak_memory_gb

    async def workspace(self, workspace_id: str) -> AuthorizedMaterials:
        association, agent = await self.association(workspace_id)
        return await self.materials(association, agent, current=True, publish_view=True)

    async def publication(self, workspace_id: str) -> PublishedView | None:
        return await asyncio.to_thread(self.views.publication, workspace_id)

    async def association(self, workspace_id: str) -> tuple[HostedWorkspace, str]:
        async with self.receiver.factory() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None or (row.task_policy or {}).get(POLICY_KEY) is None:
                raise MaterialDeniedError("HOSTED_WORKSPACE_REQUIRED", 403)
            if row.status == "destroyed" or row.filesystem_reclaimed_at is not None:
                raise MaterialDeniedError("HOSTED_WORKSPACE_INACTIVE", 403)
            association = _stored_association(row.task_policy[POLICY_KEY])
            agent = row.agent
        if association.workspace_id != workspace_id:
            raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
        return association, agent

    async def retained_workspaces(
        self, after: str = "", *, historical: bool = False, limit: int = 64
    ) -> list[str]:
        retired = or_(
            Workspace.status.in_(_HISTORICAL_STATUSES),
            Workspace.filesystem_reclaimed_at.is_not(None),
        )
        async with self.receiver.factory() as session:
            return list(
                await session.scalars(
                    select(Workspace.id)
                    .where(
                        Workspace.node_id == self.receiver.identity.owner,
                        retired if historical else ~retired,
                        Workspace.task_policy[POLICY_KEY].is_not(None),
                        Workspace.id > after,
                    )
                    .order_by(Workspace.id)
                    .limit(limit)
                )
            )

    async def reconcile_workspace(self, workspace_id: str) -> bool:
        publication = await self.publication(workspace_id)
        async with self.receiver.factory() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None or row.node_id != self.receiver.identity.owner:
                return False
            inactive = row.status == "destroyed" or row.filesystem_reclaimed_at is not None
        if inactive:
            # Destroy/GC already completed runtime cleanup. Reclaim only the
            # readable view; never republish or restart an inactive workspace.
            await self.retract(workspace_id)
        else:
            # Checkout can publish before Compose placement. Sweep every owned
            # association, but never create a readable view just by polling.
            association, agent = await self.association(workspace_id)
            await self.materials(
                association,
                agent,
                current=True,
                publish_view=publication is not None,
                refresh_publication=publication,
            )
        return True

    async def retract(self, workspace_id: str, *, preserve_directory: bool = False) -> None:
        await asyncio.to_thread(
            self.views.remove, workspace_id, preserve_directory=preserve_directory
        )
        await self._retract_previous(workspace_id)

    async def retract_publication(
        self, workspace_id: str, publication: PublishedView | None
    ) -> None:
        if publication is not None:
            await asyncio.to_thread(
                self.views.remove_owned,
                workspace_id,
                publication,
            )
        await self._retract_previous(workspace_id)

    async def _retract_previous(self, workspace_id: str) -> None:
        # A restarted owner can encounter a retained stack with its previous
        # boot's readable view. Only that row's exact older binding is removable.
        await self.receiver.identity_status()
        async with self.receiver.factory() as session:
            row = await session.get(Workspace, workspace_id)
            if row is None or row.node_id != self.receiver.identity.owner:
                return
            stored = (row.task_policy or {}).get(POLICY_KEY)
            if stored is None:
                return
            association = _stored_association(stored)
        previous = association.credentials.agent.identity
        if (
            association.workspace_id == workspace_id
            and previous.owner == self.receiver.identity.owner
            and previous.boot_epoch < self.receiver.identity.boot_epoch
        ):
            views = RuntimeViews(self.receiver.store, identity=previous)
            await asyncio.to_thread(views.remove, workspace_id)

    async def profile_mounts(
        self,
        profile: WorkspaceProfile,
        workspace_id: str,
    ) -> LocalSecretLeaseResolution:
        association, _ = await self.association(workspace_id)
        await self.workspace(workspace_id)
        bindings = {
            "agent": association.credentials.agent,
            "github": association.credentials.github,
        }
        if set(profile.runtime.environment) & set(AGENT_AUTH_ENV_VARS):
            raise MaterialDeniedError("HOSTED_PROFILE_AUTH_OVERRIDE", 422)
        for secret in profile.secrets:
            binding = bindings.get(secret.name)
            if binding is None or (
                secret.provider != "protected"
                or secret.kind != "mount"
                or secret.mode != "ro"
                or secret.ref is not None
                or secret.target != binding.target
            ):
                raise MaterialDeniedError("PROTECTED_DECLARATION_MISMATCH", 422)
        # Targets are immutable in the association. Resolve actual current
        # material through the same bounded snapshot reader as every consumer.
        material = await self.workspace(workspace_id)
        path = self.receiver.store.root / self.views._key(workspace_id) / "view"
        return LocalSecretLeaseResolution(
            mounts=(AuthMount(source=str(path), target="/run/awf/secrets", mode="ro"),),
            metadata={"protected_mount_count": 2, "expires_at": material.expires_at.isoformat()},
        )

    async def _current(self, anchor: Binding) -> Binding:
        self.receiver.expected(anchor)
        async with self.receiver.factory() as session:
            await self.receiver.authority(session)
            original = await self.receiver.receipt(session, anchor)
            if original is None or original.state not in {"installed", "expired"}:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
            prep = await self.receiver.preparation(session, anchor)
            assert prep is not None
            grant = await session.scalar(
                select(Grant).where(
                    Grant.preparation == prep.id,
                    Grant.grant_id == anchor.grant_id,
                )
            )
            if grant is None or grant.revoked or grant.current is None:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
            receipt = await session.get(Receipt, grant.current)
            if receipt is None:
                raise MaterialDeniedError("PROTECTED_MATERIAL_UNAVAILABLE", 503)
            binding = Binding.model_validate(receipt.binding)
        fields = (
            "identity",
            "namespace",
            "workspace_id",
            "preparation_id",
            "purpose",
            "target",
            "grant_id",
        )
        if any(getattr(anchor, key) != getattr(binding, key) for key in fields) or (
            binding.revision < anchor.revision
            or binding.fence < anchor.fence
            or binding.authorization_generation != anchor.authorization_generation
        ):
            raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
        return binding

    async def _snapshot_status(self, bindings: dict[str, Binding]) -> datetime:
        for binding in bindings.values():
            if (await self.receiver.status(binding)).status != "current":
                raise _SnapshotUnavailableError()
        await self.admission()
        expiry = min(binding.expires_at for binding in bindings.values())
        if expiry <= datetime.now(UTC):
            raise _SnapshotUnavailableError()
        return expiry

    async def _snapshot_successor(
        self, anchors: dict[str, Binding], previous: dict[str, Binding]
    ) -> dict[str, Binding] | None:
        # Missing/expired/revoked material is not evidence of a benign cutover.
        for binding in previous.values():
            status = await self.receiver.status(binding)
            if status.status != "current" and (
                status.status != "not_accepted" or status.reason != "SUPERSEDED"
            ):
                return None
        successor = {name: await self._current(anchor) for name, anchor in anchors.items()}
        advanced = False
        for name, binding in successor.items():
            prior = previous[name]
            if binding != prior:
                if binding.revision <= prior.revision or binding.fence <= prior.fence:
                    return None
                advanced = True
        if not advanced:
            return None
        await self._snapshot_status(successor)
        return successor

    async def _read_snapshot(
        self, bindings: dict[str, Binding], workspace_id: str, agent_environment: str
    ) -> tuple[dict[str, str], dict[str, bytes]]:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "protected-runtime",
                "secrets": [
                    {
                        "name": name,
                        "provider": "protected",
                        "kind": "mount",
                        "mode": "ro",
                        "target": value.target,
                    }
                    for name, value in bindings.items()
                ],
            }
        )
        try:
            resolved = await ProtectedResolver.resolve_current(
                self.receiver, bindings, profile, workspace_id
            )
        except MaterialDeniedError as error:
            if error.reason == "PROTECTED_MATERIAL_UNAVAILABLE":
                raise _SnapshotUnavailableError() from error
            raise
        values: dict[str, str] = {}
        files: dict[str, bytes] = {}
        for name, mount in zip(bindings, resolved.mounts, strict=True):
            path = Path(mount.source)
            try:
                raw = await asyncio.to_thread(self.receiver.store.read, path.parent.name, path.stem)
            except FileNotFoundError as error:
                raise _SnapshotUnavailableError() from error
            try:
                value = raw.decode("ascii")
            except UnicodeError:
                raise MaterialDeniedError("INVALID_API_KEY_MATERIAL", 422) from None
            if re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value) is None:
                raise MaterialDeniedError("INVALID_API_KEY_MATERIAL", 422)
            values[agent_environment if name == "agent" else "GH_TOKEN"] = value
            files[Path(bindings[name].target).name] = raw
        return values, files

    async def materials(
        self,
        association: HostedWorkspace,
        agent: str,
        *,
        current: bool,
        publish_view: bool = False,
        refresh_publication: PublishedView | None = None,
    ) -> AuthorizedMaterials:
        await self.admission()
        pair = AGENT_TARGETS.get(agent)
        if (
            pair is None
            or association.credentials.agent.target != pair[0]
            or (association.credentials.github.target != "/run/awf/secrets/github.api-key")
        ):
            raise MaterialDeniedError("HOSTED_PROVIDER_BINDING_MISMATCH", 422)
        anchors = {
            "agent": association.credentials.agent,
            "github": association.credentials.github,
        }
        retry_publication = refresh_publication
        absence = None
        if publish_view and retry_publication is None:
            owner = owned_publication(self.views, association.workspace_id)
            prior = owner.publication if owner is not None else None
            if prior is not None:
                if prior.owner is owner:
                    retry_publication = prior
            else:
                absence = await asyncio.to_thread(
                    self.views.absence_token, association.workspace_id
                )
        bindings = (
            {name: await self._current(value) for name, value in anchors.items()}
            if current
            else anchors
        )
        publication: asyncio.Task[PublishedView | None] | None = None
        publish_allowed = publish_view

        async def publish(files: dict[str, bytes], *, retry: bool) -> PublishedView | None:
            expected = retry_publication if retry else refresh_publication
            if expected is not None:
                return await asyncio.to_thread(
                    self.views.refresh, association.workspace_id, files, expected
                )
            if retry:
                if absence is None:
                    return None
                return await asyncio.to_thread(
                    self.views.publish,
                    association.workspace_id,
                    files,
                    expected_absence=absence,
                )
            return await asyncio.to_thread(self.views.publish, association.workspace_id, files)

        try:
            attempt = 0
            while True:
                attempt += 1
                try:
                    values, files = await self._read_snapshot(
                        bindings, association.workspace_id, pair[1]
                    )
                    expires_at = await self._snapshot_status(bindings)
                    if publish_allowed and not self.views.closed:
                        publication = asyncio.create_task(publish(files, retry=attempt > 1))
                        self.views.track_writer(publication)
                        published = await asyncio.shield(publication)
                        if published is None:
                            publish_allowed = False
                        else:
                            retry_publication = published
                    await self._snapshot_status(bindings)
                    if publish_view:
                        latest, latest_agent = await self.association(association.workspace_id)
                        if latest != association or latest_agent != agent:
                            raise MaterialDeniedError("HOSTED_WORKSPACE_BINDING_MISMATCH", 403)
                    if expires_at <= datetime.now(UTC):
                        raise _SnapshotUnavailableError()
                    values["GITHUB_TOKEN"] = values["GH_TOKEN"]
                    return AuthorizedMaterials(values, expires_at)
                except _SnapshotUnavailableError:
                    if not current or attempt == 3:
                        raise
                    successor = await self._snapshot_successor(anchors, bindings)
                    if successor is None:
                        raise
                    bindings = successor
                    # Never return or republish bytes from the superseded read.
                    values, files = {}, {}
        except BaseException as error:
            if publication is None:
                raise
            cancelled = isinstance(error, asyncio.CancelledError)

            # A cancelled to_thread await does not stop its writer. Finish that
            # exact publication and retract only if it is still the current view.
            async def retract() -> None:
                published = await publication
                if published is not None:
                    await asyncio.to_thread(
                        self.views.remove,
                        association.workspace_id,
                        publication=published,
                        preserve_directory=cancelled and published.retained_directory,
                    )

            cleanup = asyncio.create_task(retract())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
