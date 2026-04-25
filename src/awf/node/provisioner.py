"""Workspace provisioner.

Bridges the control-plane state machine to node-local git and stack launchers.
Given a workspace row in ``requested``, this module:

    1. Transitions it to ``provisioning`` and commits.
    2. Creates/updates the repo mirror, then adds a worktree at a fresh branch.
    3. Resolves the workspace profile and starts the outer workspace stack.
    4. Records the assigned node, compose project name, branch, and base commit.
    5. Transitions to ``ready`` and commits.

On any failure, the workspace is transitioned to ``failed`` with the most
specific ``FailureReason`` we can derive, and the raised exception is
re-raised so the caller can log/alert.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.stack_launcher import WorkspaceStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolutionError, resolve_workspace_profile

_log = get_logger(__name__)


@dataclass(frozen=True)
class ProvisionerConfig:
    """Configuration the provisioner needs that isn't per-workspace state."""

    node_id: str
    """Identifier for the host running this provisioner (e.g. hostname)."""

    branch_prefix: str = "awf"
    """Prefix for feature branches; full branch = ``<prefix>/<workspace_id>``."""


class Provisioner:
    """Orchestrates git + state transitions for one workspace at a time.

    Stateless apart from injected dependencies — safe to share across concurrent
    workspace provisioning tasks.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        git: GitManager,
        config: ProvisionerConfig,
        stack_launcher: WorkspaceStackLauncher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._git = git
        self._config = config
        self._stack_launcher = stack_launcher

    async def provision(self, workspace_id: str) -> None:
        """Drive a workspace from ``requested`` to ``ready`` (or ``failed``).

        The DB work is split across multiple transactions so the ``provisioning``
        status is visible to observers during long-running git operations.
        """
        # 1. Claim: requested -> provisioning (short txn, quickly observable)
        async with self._session_factory() as session:
            ws = await self._load_and_claim(session, workspace_id)
            if ws is None:
                return  # Workspace disappeared or wasn't requested; nothing to do.

        # 2. Do the git work outside a DB transaction (it's slow).
        branch_name = f"{self._config.branch_prefix}/{workspace_id}"
        try:
            layout = await self._git.add_worktree(
                workspace_id=workspace_id,
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                new_branch=branch_name,
            )
            base_commit = await self._git.head_sha(workspace_id=workspace_id)
            profile_resolution = None
            if ws.resolved_profile is None:
                profile_resolution = resolve_workspace_profile(
                    worktree_path=layout.worktree_path,
                    inline_profile=ws.requested_profile,
                    profile_ref=ws.profile_ref or ws.env_profile or "auto",
                    validation_commands=list(ws.test_commands),
                )
                profile = profile_resolution.profile
            else:
                profile = WorkspaceProfile.model_validate(ws.resolved_profile)
            if self._stack_launcher is not None:
                await self._stack_launcher.launch(
                    WorkspaceStackLaunchRequest(
                        workspace_id=workspace_id,
                        layout=layout,
                        profile=profile,
                    )
                )
        except GitOperationError as exc:
            _log.error(
                "provisioner.git_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:2000],
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.infrastructure_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
            )
            raise
        except ProfileResolutionError as exc:
            _log.error(
                "provisioner.profile_resolution_failed",
                workspace_id=workspace_id,
                error=str(exc),
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.profile_resolution_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
            )
            raise
        except ComposeOperationError as exc:
            _log.error(
                "provisioner.stack_startup_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:2000],
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                failure_reason=FailureReason.service_startup_failure,
                message=str(exc)[:2000],
                from_status=WorkspaceStatus.provisioning,
            )
            raise

        # 3. Commit success: write placement metadata and transition to ready.
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - defensive; workspace removed mid-provision
                return

            persisted.node_id = self._config.node_id
            persisted.branch_name = layout.branch_name
            persisted.base_commit = base_commit
            persisted.compose_project_name = f"awf_{workspace_id}"
            if profile_resolution is not None:
                persisted.resolved_profile = profile_resolution.profile.model_dump(
                    mode="json", by_alias=True
                )
                persisted.profile_ref = persisted.profile_ref or profile_resolution.profile.name

            await repo.transition(
                persisted,
                to=WorkspaceStatus.ready,
                reason_code="PROVISIONING_COMPLETE",
            )
            await session.commit()

        _log.info(
            "provisioner.ready",
            workspace_id=workspace_id,
            node_id=self._config.node_id,
            branch=layout.branch_name,
            base_commit=base_commit,
        )

    async def _load_and_claim(self, session: AsyncSession, workspace_id: str) -> Workspace | None:
        """Transition requested -> provisioning. Returns the loaded workspace or None.

        Returns None (rather than raising) if the workspace isn't in ``requested`` —
        another worker may have already claimed it. This makes the provisioner safe
        to call at-least-once from the poll loop.
        """
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:
            _log.warning("provisioner.skip_unknown", workspace_id=workspace_id)
            return None
        if ws.status != WorkspaceStatus.requested.value:
            _log.info(
                "provisioner.skip_not_requested",
                workspace_id=workspace_id,
                status=ws.status,
            )
            return None

        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="WORKER_CLAIMED")
        await session.commit()
        return ws

    async def _mark_failed(
        self,
        *,
        workspace_id: str,
        failure_reason: FailureReason,
        message: str,
        from_status: WorkspaceStatus,
    ) -> None:
        """Best-effort transition to ``failed``.

        We swallow secondary failures here: if the DB itself is unavailable the
        caller's exception will already bubble up with the primary cause, and
        logging twice is better than masking the root error.
        """
        try:
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.get(workspace_id)
                if ws is None:  # pragma: no cover - race with destroy
                    return
                if ws.status != from_status.value:
                    # Already moved elsewhere (e.g. cancelled). Respect that.
                    return
                ws.failure_reason = failure_reason.value
                ws.failure_message = message
                await repo.transition(
                    ws,
                    to=WorkspaceStatus.failed,
                    reason_code=failure_reason.value.upper(),
                )
                await session.commit()
        except Exception:  # pragma: no cover - defensive
            _log.exception("provisioner.mark_failed_failed", workspace_id=workspace_id)
