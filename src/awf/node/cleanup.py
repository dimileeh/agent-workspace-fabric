"""Workspace cleanup — tear down the compose stack, remove the worktree.

``cleanup(workspace)`` is idempotent so cron-style retry loops can call it
without having to check whether any specific artifact still exists. Errors
from individual cleanup steps are logged but don't abort the rest of the
teardown — one stuck container must not leak a git worktree.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from awf.common.logging import get_logger
from awf.node.compose_manager import ComposeManager, ComposeOperationError, WorkspaceComposeSpec
from awf.node.git_manager import GitManager, GitOperationError

_log = get_logger(__name__)

WorkspaceCleanupStepStatus = Literal["succeeded", "failed", "skipped"]
WorkspaceCleanupStatus = Literal["succeeded", "partial", "skipped"]

CLEANUP_SUCCEEDED = "CLEANUP_SUCCEEDED"
CLEANUP_PARTIAL = "CLEANUP_PARTIAL"
CLEANUP_SKIPPED = "CLEANUP_SKIPPED"
COMPOSE_DOWN_SUCCEEDED = "COMPOSE_DOWN_SUCCEEDED"
WORKTREE_REMOVE_SUCCEEDED = "WORKTREE_REMOVE_SUCCEEDED"
WORKTREE_REMOVE_SKIPPED = "WORKTREE_REMOVE_SKIPPED"


@dataclass(frozen=True)
class WorkspaceCleanupStepResult:
    """Structured outcome for one workspace cleanup step."""

    name: str
    status: WorkspaceCleanupStepStatus
    reason_code: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"succeeded", "skipped"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class WorkspaceCleanupResult:
    """Retry-safe workspace cleanup summary.

    Iteration and equality intentionally expose failed step names so older
    callers that treated cleanup as ``list[str]`` keep working.
    """

    status: WorkspaceCleanupStatus
    reason_code: str
    steps: tuple[WorkspaceCleanupStepResult, ...] = ()

    @classmethod
    def from_steps(cls, steps: list[WorkspaceCleanupStepResult]) -> WorkspaceCleanupResult:
        frozen_steps = tuple(steps)
        if any(not step.ok for step in frozen_steps):
            return cls(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=frozen_steps,
            )
        return cls(
            status="succeeded",
            reason_code=CLEANUP_SUCCEEDED,
            steps=frozen_steps,
        )

    @classmethod
    def skipped(cls, *, reason_code: str = CLEANUP_SKIPPED) -> WorkspaceCleanupResult:
        return cls(status="skipped", reason_code=reason_code)

    @property
    def ok(self) -> bool:
        return self.status in {"succeeded", "skipped"}

    @property
    def failed_steps(self) -> tuple[WorkspaceCleanupStepResult, ...]:
        return tuple(step for step in self.steps if not step.ok)

    @property
    def completed_steps(self) -> tuple[WorkspaceCleanupStepResult, ...]:
        return tuple(step for step in self.steps if step.status == "succeeded")

    @property
    def failure_messages(self) -> tuple[str, ...]:
        return tuple(step.error or step.name for step in self.failed_steps)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "steps": [step.to_dict() for step in self.steps],
            "failed_steps": [step.to_dict() for step in self.failed_steps],
            "completed_steps": [step.to_dict() for step in self.completed_steps],
        }

    def __bool__(self) -> bool:
        return not self.ok

    def __iter__(self) -> Iterator[str]:
        return (step.name for step in self.failed_steps)

    def __contains__(self, item: object) -> bool:
        return item in tuple(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WorkspaceCleanupResult):
            return (
                self.status,
                self.reason_code,
                self.steps,
            ) == (
                other.status,
                other.reason_code,
                other.steps,
            )
        if isinstance(other, list):
            return list(self) == other
        if isinstance(other, tuple):
            return tuple(self) == other
        if isinstance(other, set):
            return set(self) == other
        return NotImplemented


class WorkspaceCleaner:
    """Teardown coordinator. Holds the git + compose managers as dependencies."""

    def __init__(self, *, git: GitManager, compose: ComposeManager) -> None:
        self._git = git
        self._compose = compose

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        """Best-effort cleanup with structured per-step outcomes.

        The worktree host path is derived from ``workspace_id`` when not
        supplied. Callers may pass it explicitly if they've already computed it
        (avoids a second directory lookup).
        """
        steps: list[WorkspaceCleanupStepResult] = []

        # Step 1: compose down (stops containers, optionally removes volumes).
        spec = WorkspaceComposeSpec(
            workspace_id=workspace_id,
            worktree_host_path=worktree_host_path or Path("/dev/null"),
        )
        project_name = compose_project_name or spec.project_name()
        try:
            if compose_file_path is not None:
                if compose_file_path.exists():
                    try:
                        down_ran = await self._compose.down_project(
                            project_name=project_name,
                            compose_file=compose_file_path,
                            workspace_id=workspace_id,
                            remove_volumes=remove_volumes,
                        )
                    except ComposeOperationError as down_exc:
                        # The compose file still exists but is stale/unusable, so
                        # ``docker compose down`` failed. Fall back to label-scoped
                        # removal (mirroring ``ComposeManager.teardown_project``) so
                        # the containers, network, and host port are still released
                        # instead of leaving a stopped-only stack behind. Only a
                        # failing fallback records a failed ``compose_down``.
                        _log.warning(
                            "cleanup.compose_down_label_fallback",
                            workspace_id=workspace_id,
                            reason_code=down_exc.reason_code,
                            stderr=down_exc.stderr[:1000],
                        )
                        await self._compose.remove_project_by_label(
                            project_name=project_name,
                            workspace_id=workspace_id,
                            remove_volumes=remove_volumes,
                        )
                    else:
                        if not down_ran:
                            # The compose file existed at the check above but
                            # vanished before ``down`` ran its own existence check
                            # (e.g. a concurrent GC removed the compose directory).
                            # ``down_project`` silently noops and returns False in
                            # that case, which would skip the teardown and let the
                            # service cancel/stop path emit terminal_runtime_released
                            # while the project's containers/network/host port are
                            # still live, so fall back to the label-scoped removal
                            # (mirroring ``ComposeManager.teardown_project``).
                            _log.warning(
                                "cleanup.compose_down_vanished_label_fallback",
                                workspace_id=workspace_id,
                                project_name=project_name,
                            )
                            await self._compose.remove_project_by_label(
                                project_name=project_name,
                                workspace_id=workspace_id,
                                remove_volumes=remove_volumes,
                            )
                else:
                    await self._compose.remove_project_by_label(
                        project_name=project_name,
                        workspace_id=workspace_id,
                        remove_volumes=remove_volumes,
                    )
            else:
                await self._compose.down(spec, remove_volumes=remove_volumes)
                await self._compose.remove_project_by_label(
                    project_name=project_name,
                    workspace_id=workspace_id,
                    remove_volumes=remove_volumes,
                )
            steps.append(
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            )
        except ComposeOperationError as exc:
            _log.warning(
                "cleanup.compose_down_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            steps.append(
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="failed",
                    reason_code=exc.reason_code,
                    error=_operation_error_detail(exc),
                )
            )

        worktree_targets = (
            (workspace_id, repo_url, "worktree_remove"),
            *(
                (companion_id, companion_repo_url, f"companion_worktree_remove:{companion_id}")
                for companion_id, companion_repo_url in companion_worktrees
            ),
        )

        # Step 2: git worktree remove (idempotent already per GitManager).
        if remove_worktree:
            for worktree_id, worktree_repo_url, step_name in worktree_targets:
                try:
                    await self._git.remove_worktree(
                        workspace_id=worktree_id,
                        repo_url=worktree_repo_url,
                    )
                    steps.append(
                        WorkspaceCleanupStepResult(
                            name=step_name,
                            status="succeeded",
                            reason_code=WORKTREE_REMOVE_SUCCEEDED,
                        )
                    )
                except GitOperationError as exc:
                    _log.warning(
                        "cleanup.git_remove_failed",
                        workspace_id=workspace_id,
                        worktree_id=worktree_id,
                        reason_code=exc.reason_code,
                        stderr=exc.stderr[:1000],
                    )
                    steps.append(
                        WorkspaceCleanupStepResult(
                            name=step_name,
                            status="failed",
                            reason_code=exc.reason_code,
                            error=_operation_error_detail(exc),
                        )
                    )
        else:
            for _, _, step_name in worktree_targets:
                steps.append(
                    WorkspaceCleanupStepResult(
                        name=step_name,
                        status="skipped",
                        reason_code=WORKTREE_REMOVE_SKIPPED,
                    )
                )

        return WorkspaceCleanupResult.from_steps(steps)


def _operation_error_detail(exc: ComposeOperationError | GitOperationError) -> str:
    return exc.stderr.strip() or exc.stdout.strip() or str(exc)
