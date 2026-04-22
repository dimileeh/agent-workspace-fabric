"""Workspace executor — drives ``ready`` → ``completed`` (or ``failed``).

Pipeline:

    ready
      └─▶ running            (agent CLI invoked inside the container)
            └─▶ validating   (test commands + Alembic if required)
                  └─▶ pushing (git push + gh pr create)
                        └─▶ completed

Failure at any step transitions to ``failed`` with a typed ``FailureReason``
and keeps the compose stack running so operators can docker-exec in for
triage. Explicit ``cleanup(workspace_id)`` is a separate operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError, get_adapter
from awf.common.commands import AsyncCommandRunner
from awf.common.logging import get_logger
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeManager
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.validation import ValidationRunner

_log = get_logger(__name__)


@dataclass(frozen=True)
class ExecutorConfig:
    """Config for WorkspaceExecutor. All paths are host-absolute."""

    worktrees_root: Path
    """Parent dir containing one subdir per workspace (``<root>/<workspace_id>``)."""

    compose_projects_root: Path
    """Where per-workspace compose.yml was rendered by the Provisioner."""

    default_models: dict[AgentRuntime, str]
    """Default LLM model to pass each adapter when the request doesn't set one."""


class WorkspaceExecutor:
    """Drives a single workspace through run → validate → push → completed."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        compose: ComposeManager,
        validation: ValidationRunner,
        pr_creator: PullRequestCreator,
        config: ExecutorConfig,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._compose = compose
        self._validation = validation
        self._pr_creator = pr_creator
        self._config = config

    async def execute(self, workspace_id: str) -> None:
        """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

        The function is idempotent in the sense that it refuses to run on a
        workspace that is not currently in ``ready`` — useful when a poll
        loop races with a manual invocation.
        """
        ws = await self._claim_ready(workspace_id)
        if ws is None:
            return

        compose_file = self._config.compose_projects_root / workspace_id / "compose.yml"
        compose_project = ws.compose_project_name or f"awf_{workspace_id}"
        worktree_path = self._config.worktrees_root / workspace_id

        # ── Step 1: agent CLI runs the task inside the container ────────────
        try:
            agent = AgentRuntime(ws.agent)
            default_model = self._config.default_models.get(agent)
            adapter = get_adapter(agent, runner=self._runner, default_model=default_model)
            await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=ws.task_prompt,
                model=default_model,
            )
            agent_exit_note = None
        except AgentRunError as exc:
            # Do NOT bail out yet. A CLI that exits non-zero — typically
            # ``claude_code`` hitting a 1-hour internal session cap and
            # returning 137 (SIGKILL), or a timeout against a flaky
            # dependency — may have left valuable uncommitted work in the
            # worktree. Coding CLIs in general don't commit on their own;
            # AWF's post-agent auto-commit is the only thing that captures
            # their edits. Log the exit code, remember it for the final
            # failure message, but let the commit + validate pipeline run.
            # If there's nothing to commit, the existing no-work check
            # fails the workspace with ``agent_failure`` below. If there
            # IS work, validation decides whether it's pushable.
            agent_exit_note = (
                f"agent CLI exited {exc.result.returncode} ({exc.reason_code}); "
                f"continuing to salvage any uncommitted work"
            )
            _log.warning(
                "executor.agent_nonzero_exit_salvaging",
                workspace_id=workspace_id,
                agent=ws.agent,
                returncode=exc.result.returncode,
            )
        except Exception as exc:  # unexpected — surface with generic reason
            _log.exception("executor.unexpected_in_agent", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected error during agent run: {exc!r}"[:2000],
            )
            return

        # ── Step 1b: capture the agent's work as a commit on the feature branch ──
        # Coding CLIs make file edits reliably but are inconsistent about git:
        # some commit, some leave changes unstaged, some commit partial subsets
        # and leave the rest dirty. AWF normalizes: after the agent exits, we
        # stage everything and commit if anything's cached. If HEAD still
        # matches the base branch afterwards, the agent produced zero change
        # and we fail with a specific reason rather than pushing nothing.
        worktree_host = self._config.worktrees_root / workspace_id

        async def _git_in_worktree(args: list[str]):  # type: ignore[no-untyped-def]
            return await self._runner.run(["git", "-C", str(worktree_host), *args])

        try:
            await _git_in_worktree(["add", "-A"])
            cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
            if cached.stdout.strip():
                commit_msg = f"awf: {ws.task_title}"[:72]
                commit_body = f"Authored by AWF workspace {workspace_id} (agent: {ws.agent}).\n"
                commit_result = await self._runner.run(
                    [
                        "git",
                        "-C",
                        str(worktree_host),
                        "commit",
                        "-m",
                        commit_msg,
                        "-m",
                        commit_body,
                    ],
                )
                if not commit_result.ok:
                    raise RuntimeError(
                        f"post-agent commit failed (exit={commit_result.returncode}): "
                        f"{commit_result.stderr}"
                    )
            # Regardless of whether we just committed, verify HEAD has advanced
            # past the base commit. If not, the agent produced no change.
            rev_count = await _git_in_worktree(["rev-list", "--count", f"{ws.base_commit}..HEAD"])
            if not rev_count.ok or int(rev_count.stdout.strip() or "0") == 0:
                base_short = ws.base_commit[:10] if ws.base_commit else "unknown"
                message = (
                    f"agent exited without producing any commits on the feature branch "
                    f"(base={base_short})"
                )
                if agent_exit_note is not None:
                    message = f"{message}; {agent_exit_note}"
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=FailureReason.agent_failure,
                    message=message,
                )
                return

            # Some agents sever git history (e.g. by accidentally running
            # ``git checkout --orphan`` or by re-initialising the repo).
            # rev-list counts HIGH in that case (every HEAD commit is "new"
            # w.r.t. base because there's no shared ancestor), so the
            # previous check wouldn't notice. Without this guard, the push
            # succeeds but ``gh pr create`` dies with a cryptic
            # ``branch has no history in common with <base>`` error.
            #
            # Recovery: ``git reset --soft <base>`` moves HEAD to the base
            # commit while leaving the index untouched — the index still
            # reflects the orphan's tree. A fresh ``git commit`` then
            # produces a single commit on top of base that contains the
            # cumulative diff, and the branch is reattached to a valid
            # ancestry so the PR can be opened normally.
            ancestor = await _git_in_worktree(
                ["merge-base", "--is-ancestor", ws.base_commit, "HEAD"]
            )
            if not ancestor.ok:
                _log.warning(
                    "executor.orphan_history_detected",
                    workspace_id=workspace_id,
                    base_commit=ws.base_commit,
                )
                reset = await _git_in_worktree(["reset", "--soft", ws.base_commit])
                if reset.ok:
                    recovery_msg = f"awf: {ws.task_title} (recovered from orphan)"[:72]
                    recovery_body = (
                        f"AWF detected orphan history on workspace {workspace_id} "
                        f"(agent: {ws.agent}) and squashed the cumulative diff "
                        f"onto base commit {ws.base_commit[:10]}.\n"
                    )
                    recover_commit = await self._runner.run(
                        [
                            "git",
                            "-C",
                            str(worktree_host),
                            "commit",
                            "-m",
                            recovery_msg,
                            "-m",
                            recovery_body,
                        ],
                    )
                    if recover_commit.ok:
                        ancestor = await _git_in_worktree(
                            ["merge-base", "--is-ancestor", ws.base_commit, "HEAD"]
                        )
                if not ancestor.ok:
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.agent_failure,
                        message=(
                            "agent severed git history — HEAD does not descend from "
                            f"base commit {ws.base_commit[:10] if ws.base_commit else 'unknown'}, "
                            "and automatic recovery (reset --soft + fresh commit) also failed. "
                            "The coding CLI likely ran `git checkout --orphan` or reinitialised "
                            "the repo; inspect the worktree manually."
                        ),
                    )
                    return
                _log.info(
                    "executor.orphan_history_recovered",
                    workspace_id=workspace_id,
                    base_commit=ws.base_commit,
                )
        except Exception as exc:  # unexpected — mark infrastructure
            _log.exception("executor.commit_step_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"post-agent commit step failed: {exc!r}"[:2000],
            )
            return

        # ── Step 2: validation (tests + optional Alembic) ───────────────────
        await self._transition(workspace_id, to=WorkspaceStatus.validating, reason="AGENT_RUN_OK")

        val_result = await self._validation.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            test_commands=list(ws.test_commands),
            requires_database=ws.requires_database,
        )
        if not val_result.all_passed:
            first_fail = val_result.first_failure
            _log.info(
                "executor.validation_failed",
                workspace_id=workspace_id,
                failed_command=first_fail.command if first_fail else None,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.validating,
                failure_reason=FailureReason.validation_failure,
                message=(
                    f"validation failed: {first_fail.command}"
                    if first_fail
                    else "validation failed"
                )[:2000],
            )
            return

        # ── Step 3: push + open PR ──────────────────────────────────────────
        await self._transition(workspace_id, to=WorkspaceStatus.pushing, reason="VALIDATION_OK")

        pr_title = ws.task_title
        pr_body = _build_pr_body(ws)

        try:
            pr = await self._pr_creator.push_and_open(
                worktree_path=worktree_path,
                branch_name=ws.branch_name or f"awf/{workspace_id}",
                base_branch=ws.branch_base,
                title=pr_title,
                body=pr_body,
            )
        except PullRequestError as exc:
            _log.error(
                "executor.pr_failed",
                workspace_id=workspace_id,
                operation=exc.operation,
                returncode=exc.returncode,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.pushing,
                failure_reason=FailureReason.infrastructure_failure,
                message=str(exc)[:2000],
            )
            return

        # ── Step 4: persist PR URL + transition to completed ────────────────
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - destroyed mid-flight
                return
            persisted.pr_url = pr.url
            await repo.transition(persisted, to=WorkspaceStatus.completed, reason_code="PR_OPENED")
            await session.commit()

        _log.info(
            "executor.completed",
            workspace_id=workspace_id,
            pr_url=pr.url,
        )

    # ── Internals ──────────────────────────────────────────────────────────

    async def _claim_ready(self, workspace_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                _log.warning("executor.skip_unknown", workspace_id=workspace_id)
                return None
            if ws.status != WorkspaceStatus.ready.value:
                _log.info(
                    "executor.skip_not_ready",
                    workspace_id=workspace_id,
                    status=ws.status,
                )
                return None
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="EXECUTOR_CLAIMED")
            await session.commit()
            # Return a detached snapshot (session closes).
            return ws

    async def _transition(self, workspace_id: str, *, to: WorkspaceStatus, reason: str) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                return
            await repo.transition(ws, to=to, reason_code=reason)
            await session.commit()

    async def _mark_failed(
        self,
        *,
        workspace_id: str,
        from_status: WorkspaceStatus,
        failure_reason: FailureReason,
        message: str,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover
                return
            if ws.status != from_status.value:
                # Already moved (e.g. cancelled) — respect it.
                return
            ws.failure_reason = failure_reason.value
            ws.failure_message = message
            await repo.transition(
                ws,
                to=WorkspaceStatus.failed,
                reason_code=failure_reason.value.upper(),
            )
            await session.commit()


def _build_pr_body(ws: Workspace) -> str:
    """Standard PR description generated from the workspace's task metadata."""
    external_id = f"\n**External task ID**: {ws.task_external_id}" if ws.task_external_id else ""
    return (
        f"Automatically opened by AWF workspace `{ws.id}` "
        f"(agent: `{ws.agent}`).\n"
        f"{external_id}\n\n"
        f"### Task\n{ws.task_prompt}\n\n"
        f"---\nValidation: "
        f"{len(ws.test_commands)} test command(s) passed inside the workspace container.\n"
    )
