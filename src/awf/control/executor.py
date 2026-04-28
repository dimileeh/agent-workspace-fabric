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

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import (
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
    AgentAdapter,
    AgentDefaults,
    AgentRunError,
    get_adapter,
)
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS, defaults_with_model_overrides
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.git_identity import git_identity_config_args, git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.control.quality_gates import (
    find_protected_quality_gate_changes,
    quality_gate_violation_message,
)
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonRepository,
    TaskAttemptRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.logs import LogStore
from awf.runtime.planning import (
    PlanConformanceReport,
    build_conformance_prompt,
    build_execution_prompt,
    build_planning_prompt,
    changed_paths_from_porcelain,
    parse_conformance_report,
    render_workspace_path,
)
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.pr_monitor_operations import (
    MonitorOperationHandle,
    build_monitor_operation_payload,
    create_or_start_monitor_operation,
    finish_monitor_operation,
    monitor_operation_idempotency_key,
)
from awf.runtime.validation import ValidationCoverageResult, ValidationResult, ValidationRunner
from awf.runtime.validation_identity import (
    environment_identity_digest,
    environment_identity_inputs,
    resolved_profile_digest,
)


class _MonitorRunnerProto(Protocol):
    """Minimum surface the executor needs from a PR monitor runner.

    Declared as a Protocol so the executor doesn't structurally depend
    on ``PullRequestMonitorRunner`` — tests can pass a tiny stub, and
    the monitor stage is a clean extension seam for Phase 2 variants
    (merge queue, release-PR monitor, etc.)."""

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None: ...


_log = get_logger(__name__)

WORKTREE_MISSING_REASON_CODE = "WORKTREE_MISSING"

_RECOVERY_ACTIVE_OPERATION_STATUSES = {
    OperationStatus.pending.value,
    OperationStatus.running.value,
}
_VALIDATE_ONLY_RECOVERY_SOURCES = {"pr_monitor", "operator_api"}
_VALIDATE_ONLY_RECOVERY_MODES = {"validate_only", "rebase_only"}


@dataclass(frozen=True)
class _RebaseRecoveryResult:
    base_sha: str
    head_sha: str


class _MonitorRebaseRecoveryError(RuntimeError):
    """Raised when monitor-driven rebase recovery cannot update the PR branch."""


def _get_active_recovery_payload(workspace: Any) -> dict[str, Any] | None:
    """Return the active validate-only recovery payload (or ``None``).

    Recovery operations use a pending/running ``validate`` operation with
    ``recovery_mode`` set. The executor uses that as the discriminator that
    separates recovery from a fresh feature-execution pass.
    """
    operations = getattr(workspace, "operations", None) or []
    for operation in operations:
        if operation.status not in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            continue
        if getattr(operation, "type", None) != OperationType.validate.value:
            continue
        payload = operation.payload
        if not _is_validate_only_recovery_payload(payload):
            continue
        return cast(dict[str, Any], payload)
    return None


def _is_validate_only_recovery_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("source") in _VALIDATE_ONLY_RECOVERY_SOURCES
        and payload.get("recovery_mode") in _VALIDATE_ONLY_RECOVERY_MODES
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


@dataclass(frozen=True)
class ExecutorConfig:
    """Config for WorkspaceExecutor. All paths are host-absolute."""

    worktrees_root: Path
    """Parent dir containing one subdir per workspace (``<root>/<workspace_id>``)."""

    compose_projects_root: Path
    """Where per-workspace compose.yml was rendered by the Provisioner."""

    default_models: Mapping[AgentRuntime, str] | None = None
    """Legacy model-only overrides. Prefer ``agent_defaults`` for new code."""

    agent_defaults: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS
    """Default model and effort policy for each agent runtime."""

    agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS
    """Maximum wall-clock seconds for one agent CLI run. Default: 7200 seconds."""

    agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    """Maximum seconds with no agent stdout/stderr. Default: 900 seconds."""

    max_validation_fix_passes: int = 5
    """Maximum fix attempts on validation failure. After the initial agent
    run + validation, if validation fails, the executor re-invokes the
    coding CLI with a fix prompt (failing command + stdout/stderr tails)
    and re-validates. ``0`` disables the loop (single-shot legacy
    behaviour); the default mirrors the PR monitor's fix-cycle cap."""


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
        pr_monitor: _MonitorRunnerProto | None = None,
        pr_monitor_factory: Callable[..., _MonitorRunnerProto] | None = None,
        log_store: LogStore | None = None,
    ) -> None:
        """``pr_monitor`` and ``pr_monitor_factory`` are mutually exclusive
        optional hooks that wire the ``monitoring_pr`` stage:

        * ``pr_monitor`` — a pre-constructed monitor. Used by tests that
          hand in a stub (the production monitor needs the per-task agent
          adapter, which the executor only has mid-``execute``).
        * ``pr_monitor_factory`` — a callable the executor invokes AFTER
          the adapter is resolved. Production path: ``run_awf.py`` passes
          a factory that builds a ``PullRequestMonitorRunner`` from the
          adapter, GitHub client, worktree paths, and resolved workspace
          profile. Adapter-only factories are still accepted for older
          tests and compatibility scripts.

        If both are None the monitor stage is skipped and the executor
        preserves the original ``pushing → completed`` contract (the
        executor_tests no-monitor scenarios still pass)."""
        if pr_monitor is not None and pr_monitor_factory is not None:
            raise ValueError("pr_monitor and pr_monitor_factory are mutually exclusive")
        self._session_factory = session_factory
        self._runner = runner
        self._compose = compose
        self._validation = validation
        self._pr_creator = pr_creator
        self._config = config
        self._pr_monitor = pr_monitor
        self._pr_monitor_factory = pr_monitor_factory
        self._log_store = log_store

    async def execute(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

        The function is idempotent in the sense that it refuses to run on a
        workspace that is not currently in ``ready`` — useful when a poll
        loop races with a manual invocation.
        """
        ws = await self._claim_ready(
            workspace_id,
            execution_owner_id=execution_owner_id,
            execution_lease_expires_at=execution_lease_expires_at,
        )
        if ws is None:
            return
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="execute",
        ):
            return

        compose_file = (
            Path(ws.compose_file_path)
            if ws.compose_file_path
            else self._config.compose_projects_root / workspace_id / "compose.yml"
        )
        compose_project = ws.compose_project_name or f"awf_{workspace_id}"
        worktree_path = self._config.worktrees_root / workspace_id

        # ── Step 1: agent CLI runs the task inside the container ────────────
        # When the PR monitor's RECOVERY_DISPATCH path delivered this
        # workspace, the executor must NOT re-run planning, the agent
        # CLI, or any post-agent commit hooks — those would rewrite the
        # plan artifact and re-implement the feature mid-merge. Recovery
        # only re-runs validation against the already-pushed work.
        recovery = _get_active_recovery_payload(ws)
        rebase_recovery_result: _RebaseRecoveryResult | None = None
        baseline_coverage: ValidationCoverageResult | None = None
        try:
            agent = AgentRuntime(ws.agent)
            defaults = self._defaults_for(agent)
            adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
            default_model = adapter_defaults.model if adapter_defaults is not None else None
            adapter = get_adapter(
                agent,
                runner=self._runner,
                defaults=adapter_defaults,
                log_store=self._log_store,
                agent_wall_timeout_seconds=self._config.agent_wall_timeout_seconds,
                agent_idle_timeout_seconds=self._config.agent_idle_timeout_seconds,
            )
            profile = _profile_for_workspace(ws, worktree_path=worktree_path)
            setup_result = await self._validation.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=("setup", "pre_agent"),
            )
            if not setup_result.all_passed:
                first_fail = setup_result.first_failure
                if recovery is not None:
                    await self._finish_active_recovery_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        reason_code="MONITOR_RECOVERY_SETUP_FAILED",
                        error_message=(
                            f"profile setup failed: {first_fail.command}"
                            if first_fail is not None
                            else "profile setup failed"
                        )[:2000],
                    )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.running,
                    failure_reason=_failure_reason_for_phase(first_fail),
                    message=(
                        f"profile setup failed: {first_fail.command}"
                        if first_fail is not None
                        else "profile setup failed"
                    )[:2000],
                )
                return
            if recovery is None:
                baseline_coverage = await self._run_baseline_coverage_preflight(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                )
                if not await self._recheck_status(
                    workspace_id,
                    expected=WorkspaceStatus.running,
                    action="agent_run",
                ):
                    return
                planning_error = await self._run_agent_task_with_optional_planning(
                    adapter=adapter,
                    workspace=ws,
                    profile=profile,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    worktree_path=worktree_path,
                    model=default_model,
                )
                if planning_error is not None:
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.agent_failure,
                        message=planning_error[:2000],
                    )
                    return
            else:
                # Recovery dispatch created the validate Operation in ``pending``;
                # flush it to ``running`` before validation so observability
                # tooling sees a real ``started_at`` (otherwise the row jumps
                # straight from pending → succeeded/failed when the validate
                # finalizer fires, with started_at == finished_at).
                await self._start_pending_recovery_operations(
                    workspace_id=workspace_id,
                )
                _log.info(
                    "executor.validate_only_recovery_started",
                    workspace_id=workspace_id,
                    source=recovery.get("source"),
                    recovery_mode=recovery.get("recovery_mode"),
                    reason=recovery.get("reason"),
                )
            agent_exit_note = None
        except ComposeExecCleanupError as exc:
            _log.error(
                "executor.exec_process_cleanup_failed",
                workspace_id=workspace_id,
                source=exc.source,
                label=exc.label,
                invocation_id=exc.invocation_id,
                reason_code=exc.reason_code,
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return
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
                reason_code=exc.reason_code,
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
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="post_agent_commit",
        ):
            return

        # Coding CLIs make file edits reliably but are inconsistent about git:
        # some commit, some leave changes unstaged, some commit partial subsets
        # and leave the rest dirty. AWF normalizes: after the agent exits, we
        # stage everything and commit if anything's cached. If HEAD still
        # matches the base branch afterwards, the agent produced zero change
        # and we fail with a specific reason rather than pushing nothing.
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.running,
            action="post_agent_commit",
        ):
            return

        # ``base_commit`` is set by the provisioner before a workspace ever
        # reaches ``ready`` — if it's missing here something went wrong
        # upstream and every ``rev-list``/``merge-base`` below would
        # inject the literal string "None" into a git command. Fail
        # cleanly instead of passing "None..HEAD" to git
        # (review feedback on #2: gemini, coderabbit).
        if ws.base_commit is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "workspace has no base_commit — provisioning must set "
                    "this before the agent run; cannot verify feature-branch "
                    "commits without it"
                ),
            )
            return
        base_commit: str = ws.base_commit

        async def _git_in_worktree(args: list[str]):  # type: ignore[no-untyped-def]
            return await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            )

        expected_branch = ws.branch_name or f"awf/{workspace_id}"

        try:
            if recovery is None:
                # ── Branch-drift recovery ──────────────────────────────────
                # Agent CLIs (Claude Code, Codex) sometimes run
                # ``git checkout -b <descriptive-name>`` mid-session as part
                # of "good git hygiene" — they don't know AWF already
                # created the right branch for them. If they commit on the
                # drifted branch, ``pr_creator.push_and_open`` pushes the
                # original (empty) AWF branch to origin and ``gh pr create``
                # fails with "No commits between development and awf/ws_...".
                #
                # Incident 2026-04-24 (T41 Phase 3, ws_9ca6134a): agent
                # switched to ``awf/t41-phase3-github-app-install-flow``
                # and committed there. AWF's push of the empty
                # ``awf/ws_9ca6134a...`` created a no-op PR. Agent's work
                # stranded in the worktree.
                #
                # Recovery: if HEAD's branch diverged, fast-forward the
                # expected branch to the agent's tip. Both branches share
                # the same base commit (the worktree was created fresh
                # from origin/<base>), so this is a safe pointer update.
                current_branch_r = await _git_in_worktree(["rev-parse", "--abbrev-ref", "HEAD"])
                # If rev-parse itself failed (corrupted git state, missing
                # HEAD, etc.) we can't reliably detect drift. Fail loudly
                # rather than silently skip — a working tree that can't
                # resolve HEAD is broken enough that continuing to push
                # would produce nonsense anyway.
                if not current_branch_r.ok:
                    raise RuntimeError(
                        "branch drift check: ``git rev-parse --abbrev-ref HEAD`` "
                        f"failed with exit {current_branch_r.returncode}: "
                        f"{current_branch_r.stderr!r}"
                    )
                current_branch = (current_branch_r.stdout or "").strip()
                if current_branch and current_branch != expected_branch:
                    _log.warning(
                        "executor.branch_drift_detected",
                        workspace_id=workspace_id,
                        current_branch=current_branch,
                        expected_branch=expected_branch,
                    )
                    agent_head_r = await _git_in_worktree(["rev-parse", "HEAD"])
                    agent_head = (agent_head_r.stdout or "").strip()
                    if not agent_head_r.ok or not agent_head:
                        raise RuntimeError(
                            f"branch drift detected (current={current_branch} "
                            f"expected={expected_branch}) but agent HEAD could not "
                            f"be resolved: {agent_head_r.stderr!r}"
                        )
                    # Preserve uncommitted changes. The executor's core
                    # contract is "salvage whatever the agent left on
                    # disk" — including edits not yet committed on the
                    # drifted branch. ``status --porcelain`` with
                    # ``--untracked-files=all`` catches both staged,
                    # unstaged, and untracked files. If any exist, stash
                    # them (with ``-u`` to include untracked) before the
                    # ``switch`` so they don't get lost. Pop after the
                    # fast-forward so they end up on top of the agent's
                    # commits on the expected branch.
                    status_r = await _git_in_worktree(
                        ["status", "--porcelain=v1", "--untracked-files=all"]
                    )
                    if not status_r.ok:
                        raise RuntimeError(
                            f"branch drift recovery: ``git status`` failed: {status_r.stderr!r}"
                        )
                    has_wip = bool(status_r.stdout.strip())
                    stash_created = False
                    if has_wip:
                        stash_r = await _git_in_worktree(
                            [
                                "stash",
                                "push",
                                "--include-untracked",
                                "--message",
                                f"awf-drift-recovery-{workspace_id}",
                            ]
                        )
                        if not stash_r.ok:
                            raise RuntimeError(
                                f"branch drift recovery: ``git stash push`` failed: "
                                f"{stash_r.stderr!r} (refusing to switch with dirty "
                                f"worktree that couldn't be stashed)"
                            )
                        stash_created = True

                    # Switch to the expected branch. It should exist
                    # locally — AWF created it at worktree-add time.
                    switch_r = await _git_in_worktree(["switch", expected_branch])
                    if not switch_r.ok:
                        # Best-effort: try to restore stashed WIP so it's
                        # not silently lost before bailing out.
                        if stash_created:
                            await _git_in_worktree(["stash", "pop"])
                        raise RuntimeError(
                            f"branch drift recovery: could not switch back to "
                            f"{expected_branch}: {switch_r.stderr!r}"
                        )
                    # Fast-forward the expected branch to the agent's tip
                    # using ``merge --ff-only``. The two branches share
                    # the same base (AWF created the worktree fresh from
                    # ``origin/<base>``) and the agent only added commits
                    # on top, so ff must succeed. ``merge --ff-only`` over
                    # ``reset --hard`` because the latter would also wipe
                    # any WIP the user has in the working tree if the
                    # stash step above silently did nothing (e.g. if
                    # ``status`` missed an edge case).
                    merge_r = await _git_in_worktree(["merge", "--ff-only", agent_head])
                    if not merge_r.ok:
                        if stash_created:
                            await _git_in_worktree(["stash", "pop"])
                        raise RuntimeError(
                            f"branch drift recovery: ``merge --ff-only "
                            f"{agent_head[:10]}`` failed: {merge_r.stderr!r}"
                        )

                    if stash_created:
                        pop_r = await _git_in_worktree(["stash", "pop"])
                        if not pop_r.ok:
                            # A pop conflict means the agent's WIP and the
                            # fast-forwarded commits touch the same
                            # regions. That's a real problem for the
                            # workspace — the WIP is left in the stash
                            # under a named entry, but we can't auto-merge
                            # it. Fail loudly so the operator knows to
                            # inspect.
                            raise RuntimeError(
                                f"branch drift recovery: ``git stash pop`` failed "
                                f"(WIP conflicts with recovered commits): "
                                f"{pop_r.stderr!r}"
                            )

                    _log.info(
                        "executor.branch_drift_recovered",
                        workspace_id=workspace_id,
                        recovered_from=current_branch,
                        recovered_to=expected_branch,
                        head_sha=agent_head,
                        wip_stashed=has_wip,
                    )

                await _git_in_worktree(["add", "-A"])
                cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
                if cached.stdout.strip():
                    violations = find_protected_quality_gate_changes(
                        changed_paths=_git_name_lines(cached.stdout),
                        owned_paths=list(ws.owned_paths),
                    )
                    if violations:
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.running,
                            failure_reason=FailureReason.policy_failure,
                            reason_code="QUALITY_GATE_POLICY_CHANGED",
                            message=quality_gate_violation_message(violations)[:2000],
                        )
                        return
                    commit_msg = f"awf: {ws.task_title}"[:72]
                    commit_body = f"Authored by AWF workspace {workspace_id} (agent: {ws.agent}).\n"
                    commit_result = await self._runner.run(
                        [
                            "git",
                            *git_safe_directory_config_args(worktree_path),
                            "-C",
                            str(worktree_path),
                            *git_identity_config_args(),
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
                rev_count = await _git_in_worktree(["rev-list", "--count", f"{base_commit}..HEAD"])
                if not rev_count.ok or int(rev_count.stdout.strip() or "0") == 0:
                    base_short = base_commit[:10] if base_commit else "unknown"
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
                #
                # Invariant: ``base_commit`` is always populated by
                # ``_claim_ready`` before this block runs. The ``assert`` both
                # documents and satisfies mypy.
                ancestor = await _git_in_worktree(["merge-base", "--is-ancestor", base_commit, "HEAD"])
                if not ancestor.ok:
                    _log.warning(
                        "executor.orphan_history_detected",
                        workspace_id=workspace_id,
                        base_commit=base_commit,
                    )
                    reset = await _git_in_worktree(["reset", "--soft", base_commit])
                    if reset.ok:
                        recovery_msg = f"awf: {ws.task_title} (recovered from orphan)"[:72]
                        recovery_body = (
                            f"AWF detected orphan history on workspace {workspace_id} "
                            f"(agent: {ws.agent}) and squashed the cumulative diff "
                            f"onto base commit {base_commit[:10]}.\n"
                        )
                        recover_commit = await self._runner.run(
                            [
                                "git",
                                *git_safe_directory_config_args(worktree_path),
                                "-C",
                                str(worktree_path),
                                *git_identity_config_args(),
                                "commit",
                                "-m",
                                recovery_msg,
                                "-m",
                                recovery_body,
                            ],
                        )
                        if recover_commit.ok:
                            ancestor = await _git_in_worktree(
                                ["merge-base", "--is-ancestor", base_commit, "HEAD"]
                            )
                    if not ancestor.ok:
                        await self._mark_failed(
                            workspace_id=workspace_id,
                            from_status=WorkspaceStatus.running,
                            failure_reason=FailureReason.agent_failure,
                            message=(
                                "agent severed git history — HEAD does not descend from "
                                f"base commit {base_commit[:10] if base_commit else 'unknown'}, "
                                "and automatic recovery (reset --soft + fresh commit) also failed. "
                                "The coding CLI likely ran `git checkout --orphan` or reinitialised "
                                "the repo; inspect the worktree manually."
                            ),
                        )
                        return
                    _log.info(
                        "executor.orphan_history_recovered",
                        workspace_id=workspace_id,
                        base_commit=base_commit,
                    )
            elif recovery.get("recovery_mode") == "rebase_only":
                try:
                    rebase_recovery_result = await self._run_monitor_rebase_recovery(
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        base_branch=ws.branch_base,
                        branch_name=expected_branch,
                        remote_branch=ws.remote_push_branch or expected_branch,
                        reason=str(recovery.get("reason") or "stale"),
                        recovery_payload=recovery,
                    )
                    base_commit = rebase_recovery_result.base_sha
                except _MonitorRebaseRecoveryError as exc:
                    message = str(exc)[:2000]
                    await self._finish_active_recovery_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.running,
                        failure_reason=FailureReason.infrastructure_failure,
                        message=message,
                        reason_code="MONITOR_RECOVERY_REBASE_FAILED",
                    )
                    return
        except Exception as exc:  # unexpected — mark infrastructure
            _log.exception("executor.commit_step_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.running,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"post-agent commit step failed: {exc!r}"[:2000],
            )
            return

        # ── Step 2: validation (tests + optional Alembic), with fix-cycle ──
        if not await self._transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.running,
            to=WorkspaceStatus.validating,
            reason="AGENT_RUN_OK",
            action="start_validation",
        ):
            return

        max_fix_passes = self._config.max_validation_fix_passes
        profile = _profile_for_workspace(ws, worktree_path=worktree_path)
        validation_commands = [
            command.command
            for _, command in profile.phases.commands_for(("post_agent", "validate"))
        ]
        test_commands_tuple = tuple(validation_commands)
        validation_tier = _validation_tier_for_workspace(ws, profile)
        if rebase_recovery_result is not None:
            validation_tier = max(validation_tier, 2)
        last_failure_message: str | None = None
        successful_validation_run_id: str | None = None
        for pass_number in range(max_fix_passes + 1):
            # pass_number == 0 is the initial run (already-committed agent
            # work). 1..N are fix attempts driven by the retry prompt.
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validate",
            ):
                return
            validation_run_id = await self._start_validation_run(
                workspace_id=workspace_id,
                profile=profile,
                base_commit=base_commit,
                workspace_head_sha=await self._capture_workspace_head_sha(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                ),
                target_branch=expected_branch,
                target_head_sha=None,
                tier=validation_tier,
            )
            try:
                val_result = await self._validation.run_profile_phases(
                    workspace_id=workspace_id,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    profile=profile,
                    phase_names=("post_agent", "validate"),
                    run_healthchecks=True,
                )
            except ComposeExecCleanupError as exc:
                message = cleanup_failure_message(exc)
                _log.error(
                    "executor.validation_cleanup_failed",
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                    source=exc.source,
                    label=exc.label,
                    invocation_id=exc.invocation_id,
                    reason_code=exc.reason_code,
                )
                await self._finish_validation_run(
                    validation_run_id,
                    status="failed",
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return
            except Exception as exc:
                _log.exception(
                    "executor.validation_run_unexpected_failed",
                    workspace_id=workspace_id,
                    validation_run_id=validation_run_id,
                )
                await self._finish_validation_run(
                    validation_run_id,
                    status="failed",
                    reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=f"unexpected error during validation run: {exc!r}"[:2000],
                    reason_code="VALIDATION_INFRASTRUCTURE_ERROR",
                )
                return
            val_result = _apply_baseline_coverage_ratchet(
                val_result,
                baseline_coverage=baseline_coverage,
            )
            await self._finish_validation_run(
                validation_run_id,
                status="succeeded" if val_result.all_passed else "failed",
                reason_code=_validation_run_reason_code(val_result),
                retry_count=val_result.total_retries,
                coverage=_validation_run_coverage_metadata(
                    val_result,
                    baseline_coverage=baseline_coverage,
                ),
                command_retries=[c.retry_count for c in val_result.commands],
            )
            if val_result.all_passed:
                successful_validation_run_id = validation_run_id
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.succeeded,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code="VALIDATION_OK",
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                )
                if pass_number > 0:
                    _log.info(
                        "executor.validation_recovered",
                        workspace_id=workspace_id,
                        fix_passes_used=pass_number,
                    )
                break

            first_fail = val_result.first_failure
            _log.info(
                "executor.validation_failed",
                workspace_id=workspace_id,
                failed_command=first_fail.command if first_fail else None,
                fix_pass=pass_number,
                max_fix_passes=max_fix_passes,
            )
            last_failure_message = _validation_failure_message(
                val_result,
                baseline_coverage=baseline_coverage,
            )

            if pass_number >= max_fix_passes or first_fail is None:
                # Exhausted our budget (or no failure details to anchor a
                # fix prompt on) — mark failed and let the operator triage.
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=_validation_run_reason_code(val_result),
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=last_failure_message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=_failure_reason_for_phase(first_fail),
                    message=(
                        last_failure_message
                        + (f" (after {max_fix_passes} fix attempts)" if max_fix_passes > 0 else "")
                    )[:2000],
                )
                return

            # Fire a fix pass: re-invoke the coding CLI with the failure
            # context, then re-commit whatever it changed.
            fix_context = ValidationFixContext(
                failed_command=first_fail.command,
                returncode=first_fail.returncode,
                stdout_tail=read_output_tail(first_fail.stdout_path),
                stderr_tail=read_output_tail(first_fail.stderr_path),
                pass_number=pass_number + 1,
                total_passes=max_fix_passes,
                test_commands=test_commands_tuple,
                reason_code=_validation_run_reason_code(val_result),
                coverage_percent=val_result.coverage.percent if val_result.coverage else None,
                coverage_minimum_percent=(
                    val_result.coverage.minimum_percent if val_result.coverage else None
                ),
                baseline_coverage_percent=(
                    baseline_coverage.percent if baseline_coverage is not None else None
                ),
            )
            fix_prompt = build_fix_prompt(fix_context)
            _log.info(
                "executor.fix_pass_start",
                workspace_id=workspace_id,
                pass_number=pass_number + 1,
                max_fix_passes=max_fix_passes,
                failed_command=first_fail.command,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validation_fix_agent_run",
            ):
                return
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_agent_run",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return
            try:
                await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=fix_prompt,
                    model=default_model,
                    workspace_id=workspace_id,
                )
            except ComposeExecCleanupError as exc:
                message = cleanup_failure_message(exc)
                _log.error(
                    "executor.fix_pass_cleanup_failed",
                    workspace_id=workspace_id,
                    pass_number=pass_number + 1,
                    source=exc.source,
                    label=exc.label,
                    invocation_id=exc.invocation_id,
                    reason_code=exc.reason_code,
                )
                await self._finish_pending_validate_operations(
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    coverage=_validation_run_coverage_metadata(
                        val_result,
                        baseline_coverage=baseline_coverage,
                    ),
                    error_message=message,
                )
                await self._mark_failed(
                    workspace_id=workspace_id,
                    from_status=WorkspaceStatus.validating,
                    failure_reason=FailureReason.infrastructure_failure,
                    message=message,
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                )
                return
            except AgentRunError as exc:
                # Coding CLI exited non-zero on the fix pass. Mirrors the
                # initial-run behaviour: log, remember the note, fall
                # through to commit any salvaged work, then continue the
                # loop (next validation will tell us if it's pushable).
                _log.warning(
                    "executor.fix_pass_agent_nonzero_exit",
                    workspace_id=workspace_id,
                    pass_number=pass_number + 1,
                    returncode=exc.result.returncode,
                    reason_code=exc.reason_code,
                )

            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="validation_fix_commit",
            ):
                return
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_add",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return

            # Commit whatever the fix pass produced. Simpler than the initial
            # post-agent commit block — orphan-history recovery isn't possible
            # here (HEAD already descends from base after the initial run
            # succeeded); zero-change fix passes are allowed.
            fix_add = await _git_in_worktree(["add", "-A"])
            if not fix_add.ok:
                _log.warning(
                    "executor.fix_pass_add_failed",
                    workspace_id=workspace_id,
                    stderr=fix_add.stderr[:400],
                )
            if not await self._ensure_worktree_available(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                expected=WorkspaceStatus.validating,
                action="validation_fix_git_diff",
                validation_run_id=validation_run_id,
                requested_tier=validation_tier,
            ):
                return
            fix_cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
            if fix_cached.stdout.strip():
                violations = find_protected_quality_gate_changes(
                    changed_paths=_git_name_lines(fix_cached.stdout),
                    owned_paths=list(ws.owned_paths),
                )
                if violations:
                    message = quality_gate_violation_message(violations)
                    await self._finish_pending_validate_operations(
                        workspace_id=workspace_id,
                        status=OperationStatus.failed,
                        validation_run_id=validation_run_id,
                        requested_tier=validation_tier,
                        reason_code="QUALITY_GATE_POLICY_CHANGED",
                        coverage=_validation_run_coverage_metadata(
                            val_result,
                            baseline_coverage=baseline_coverage,
                        ),
                        error_message=message,
                    )
                    await self._mark_failed(
                        workspace_id=workspace_id,
                        from_status=WorkspaceStatus.validating,
                        failure_reason=FailureReason.policy_failure,
                        reason_code="QUALITY_GATE_POLICY_CHANGED",
                        message=message[:2000],
                    )
                    return
                if not await self._ensure_worktree_available(
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    expected=WorkspaceStatus.validating,
                    action="validation_fix_git_commit",
                    validation_run_id=validation_run_id,
                    requested_tier=validation_tier,
                ):
                    return
                commit_msg = f"awf: fix pass {pass_number + 1} for {ws.task_title}"[:72]
                commit_body = (
                    f"AWF validation fix pass {pass_number + 1} of "
                    f"{max_fix_passes} for workspace {workspace_id} "
                    f"(agent: {ws.agent}). Failed command: "
                    f"{first_fail.command}."
                )
                fix_commit = await self._runner.run(
                    [
                        "git",
                        "-C",
                        str(worktree_path),
                        "commit",
                        "-m",
                        commit_msg,
                        "-m",
                        commit_body,
                    ],
                )
                if not fix_commit.ok:
                    _log.warning(
                        "executor.fix_pass_commit_failed",
                        workspace_id=workspace_id,
                        stderr=fix_commit.stderr[:400],
                    )
            # Loop back to re-validate.

        # ── Recovery skip-push guard ───────────────────────────────────────
        # Recovery for a workspace that already has an open PR must NOT
        # re-create the PR. Validate-only recovery does not push; rebase-only
        # recovery already pushed the rebased branch above and now just hands
        # back to the monitor after validation.
        if recovery is not None and ws.pr_url:
            if rebase_recovery_result is not None and successful_validation_run_id is not None:
                try:
                    await self._set_validation_run_target_head_sha(
                        validation_run_id=successful_validation_run_id,
                        target_head_sha=rebase_recovery_result.head_sha,
                    )
                    await self._clear_rebase_recovery_staleness(
                        workspace_id=workspace_id,
                    )
                except Exception:
                    _log.exception(
                        "executor.rebase_recovery_staleness_clear_failed",
                        workspace_id=workspace_id,
                        validation_run_id=successful_validation_run_id,
                    )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.validating,
                action="recovery_skip_push",
            ):
                return
            async with self._session_factory() as session:
                repo = WorkspaceRepository(session)
                persisted = await repo.get(workspace_id)
                if persisted is None:  # pragma: no cover - destroyed mid-flight
                    return
                if persisted.status != WorkspaceStatus.validating.value:
                    await self._record_stale_action_skip(
                        repo,
                        persisted,
                        action="recovery_skip_push",
                        expected=WorkspaceStatus.validating,
                        reason_code="EXECUTOR_STALE_STATUS",
                    )
                    await session.commit()
                    return
                has_monitor = (
                    self._pr_monitor is not None or self._pr_monitor_factory is not None
                )
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.monitoring_pr
                    if has_monitor
                    else WorkspaceStatus.completed,
                    reason_code="RECOVERY_VALIDATION_OK",
                )
                await session.commit()
            _log.info(
                "executor.recovery_skip_push",
                workspace_id=workspace_id,
                pr_url=ws.pr_url,
                has_monitor=has_monitor,
            )
            if has_monitor:
                _monitor: _MonitorRunnerProto | None = self._pr_monitor
                if _monitor is None and self._pr_monitor_factory is not None:
                    _monitor = _call_pr_monitor_factory(
                        self._pr_monitor_factory,
                        adapter=adapter,
                        profile=profile,
                        workspace=persisted,
                    )
                if _monitor is not None:
                    _log.info(
                        "executor.recovery_handoff_to_pr_monitor",
                        workspace_id=workspace_id,
                        pr_url=ws.pr_url,
                    )
                    if not await self._recheck_status(
                        workspace_id,
                        expected=WorkspaceStatus.monitoring_pr,
                        action="run_pr_monitor",
                    ):
                        return
                    await _monitor.run(
                        workspace_id=workspace_id,
                        compose_project=compose_project,
                        compose_file=compose_file,
                    )
            return

        # ── Step 3: push + open PR ──────────────────────────────────────────
        if not await self._transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.validating,
            to=WorkspaceStatus.pushing,
            reason="VALIDATION_OK",
            action="start_push",
        ):
            return
        if not await self._ensure_worktree_available(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            expected=WorkspaceStatus.pushing,
            action="pr_push_open",
        ):
            return

        pr_title = ws.task_title
        pr_body = _build_pr_body(ws, defaults=defaults)

        try:
            pr = await self._pr_creator.push_and_open(
                worktree_path=worktree_path,
                branch_name=ws.branch_name or f"awf/{workspace_id}",
                base_branch=ws.branch_base,
                title=pr_title,
                body=pr_body,
                existing_pr_url=ws.pr_url,
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
        except Exception as exc:
            _log.exception("executor.pr_unexpected_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.pushing,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"unexpected error during PR creation: {exc!r}"[:2000],
            )
            return

        # ── Step 4: persist PR URL + (optionally) hand off to monitor ──────
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - destroyed mid-flight
                return
            if persisted.status != WorkspaceStatus.pushing.value:
                await self._record_stale_action_skip(
                    repo,
                    persisted,
                    action="persist_pr",
                    expected=WorkspaceStatus.pushing,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return
            had_existing_pr_url = bool(persisted.pr_url)
            persisted.pr_url = pr.url
            persisted.pr_number = _extract_pr_number(pr.url)
            if pr.head_sha:
                persisted.monitor_last_commit_sha = pr.head_sha
            if persisted.task_kind == "feature_branch_pr" and not persisted.remote_push_branch:
                persisted.remote_push_branch = (
                    pr.branch or persisted.branch_name or f"awf/{workspace_id}"
                )
            # Resolve which monitor (if any) to hand off to. Pre-constructed
            # ``pr_monitor`` wins (tests); otherwise the factory builds one
            # from the per-task adapter now that we have it.
            monitor: _MonitorRunnerProto | None = self._pr_monitor
            if monitor is None and self._pr_monitor_factory is not None:
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                    workspace=persisted,
                )

            if monitor is not None:
                # Hand off to the monitor — it will transition to completed
                # (on merge) or failed (on abort / cap / close).
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.monitoring_pr,
                    reason_code="PR_UPDATED" if had_existing_pr_url else "PR_OPENED",
                )
                await session.commit()
            else:
                # No monitor wired (legacy executor path / unit-test shim) —
                # preserve the original ``pushing → completed`` contract.
                await repo.transition(
                    persisted,
                    to=WorkspaceStatus.completed,
                    reason_code="PR_UPDATED" if had_existing_pr_url else "PR_OPENED",
                )
                await session.commit()

        if successful_validation_run_id is not None and pr.head_sha:
            try:
                await self._set_validation_run_target_head_sha(
                    validation_run_id=successful_validation_run_id,
                    target_head_sha=pr.head_sha,
                )
            except Exception:
                _log.exception(
                    "executor.validation_run_target_head_sha_update_failed",
                    workspace_id=workspace_id,
                    validation_run_id=successful_validation_run_id,
                    target_head_sha=pr.head_sha,
                )

        if monitor is not None:
            _log.info(
                "executor.handoff_to_pr_monitor",
                workspace_id=workspace_id,
                pr_url=pr.url,
            )
            if not await self._recheck_status(
                workspace_id,
                expected=WorkspaceStatus.monitoring_pr,
                action="run_pr_monitor",
            ):
                return
            await monitor.run(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return

        _log.info(
            "executor.completed",
            workspace_id=workspace_id,
            pr_url=pr.url,
        )

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        """Resume the PR monitor for a workspace already in ``monitoring_pr``.

        This is the service-worker restart path. It intentionally skips setup,
        agent execution, validation, push, and PR creation; those have already
        happened before the workspace entered ``monitoring_pr``.
        """
        ws = await self._load_workspace(workspace_id)
        if ws is None:
            _log.warning("executor.resume_skip_unknown", workspace_id=workspace_id)
            return
        if ws.status != WorkspaceStatus.monitoring_pr.value:
            _log.info(
                "executor.resume_skip_not_monitoring_pr",
                workspace_id=workspace_id,
                status=ws.status,
            )
            return
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_pr_monitor",
        ):
            return

        if not ws.remote_push_branch and ws.task_kind == "feature_branch_pr" and ws.branch_name:
            recovered_remote_push_branch = await self._recover_feature_branch_remote_push_branch(
                workspace_id=workspace_id,
                remote_push_branch=ws.branch_name,
            )
            if recovered_remote_push_branch:
                ws.remote_push_branch = recovered_remote_push_branch

        missing = _missing_monitor_recovery_metadata(ws)
        if missing:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message=(
                    "monitor recovery: missing required persisted metadata: " + ", ".join(missing)
                )[:2000],
                reason_code="MONITOR_RECOVERY_METADATA_MISSING",
            )
            return

        compose_project = ws.compose_project_name
        compose_file_path = ws.compose_file_path
        assert compose_project is not None
        assert compose_file_path is not None

        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_compose",
        ):
            return

        try:
            await self._compose.ensure_project_up(
                project_name=compose_project,
                compose_file=Path(compose_file_path),
                workspace_id=workspace_id,
                wait=True,
            )
        except ComposeOperationError as exc:
            _log.error(
                "executor.resume_compose_up_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"monitor recovery: compose stack failed to start: {exc}"[:2000],
                reason_code="MONITOR_RECOVERY_COMPOSE_FAILED",
            )
            return

        monitor: _MonitorRunnerProto | None = self._pr_monitor
        try:
            if monitor is None and self._pr_monitor_factory is not None:
                agent = AgentRuntime(ws.agent)
                defaults = self._defaults_for(agent)
                adapter_defaults = _agent_defaults_for_workspace(ws, defaults)
                adapter = get_adapter(
                    agent,
                    runner=self._runner,
                    defaults=adapter_defaults,
                    log_store=self._log_store,
                )
                profile = _profile_for_workspace(
                    ws,
                    worktree_path=self._config.worktrees_root / workspace_id,
                )
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                    workspace=ws,
                )
        except Exception as exc:
            _log.exception("executor.pr_monitor_resume_build_failed", workspace_id=workspace_id)
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message=f"monitor recovery: failed to build PR monitor: {exc!r}"[:2000],
                reason_code="MONITOR_RECOVERY_FAILED",
            )
            return

        if monitor is None:
            await self._mark_failed(
                workspace_id=workspace_id,
                from_status=WorkspaceStatus.monitoring_pr,
                failure_reason=FailureReason.infrastructure_failure,
                message="monitor recovery: no PR monitor configured",
                reason_code="MONITOR_RECOVERY_FAILED",
            )
            return

        _log.info(
            "executor.resume_pr_monitor",
            workspace_id=workspace_id,
            pr_url=ws.pr_url,
            pr_number=ws.pr_number,
        )
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.monitoring_pr,
            action="resume_monitor_run",
        ):
            return
        await monitor.run(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=Path(compose_file_path),
        )

    # ── Internals ──────────────────────────────────────────────────────────

    async def _load_workspace(self, workspace_id: str) -> Workspace | None:
        async with self._session_factory() as session:
            return await WorkspaceRepository(session).get(workspace_id)

    async def _recover_feature_branch_remote_push_branch(
        self,
        *,
        workspace_id: str,
        remote_push_branch: str,
    ) -> str | None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
                return None
            if ws.remote_push_branch:
                return ws.remote_push_branch
            if ws.task_kind != "feature_branch_pr" or not ws.branch_name:
                return None
            ws.remote_push_branch = remote_push_branch
            await repo.add_event(
                ws,
                event_type="workspace.remote_push_branch_recovered",
                reason_code="REMOTE_PUSH_BRANCH_RECOVERED",
                payload={
                    "remote_push_branch": remote_push_branch,
                    "source": "branch_name",
                },
            )
            await session.commit()
            return remote_push_branch

    async def _ensure_worktree_available(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        expected: WorkspaceStatus,
        action: str,
        validation_run_id: str | None = None,
        requested_tier: int | None = None,
    ) -> bool:
        if worktree_path.is_dir():
            return True

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - row disappeared mid-flight
                return False
            if ws.status != expected.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action=action,
                    expected=expected,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return False

            message = _worktree_missing_message(worktree_path, action)
            _log.error(
                "executor.worktree_missing",
                workspace_id=workspace_id,
                action=action,
                worktree_path=str(worktree_path),
                reason_code=WORKTREE_MISSING_REASON_CODE,
            )
            await repo.add_event(
                ws,
                event_type="workspace.executor_worktree_missing",
                reason_code=WORKTREE_MISSING_REASON_CODE,
                payload={
                    "action": action,
                    "worktree_path": str(worktree_path),
                },
            )
            if validation_run_id is not None and requested_tier is not None:
                await self._finish_pending_validate_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    status=OperationStatus.failed,
                    validation_run_id=validation_run_id,
                    requested_tier=requested_tier,
                    reason_code=WORKTREE_MISSING_REASON_CODE,
                    error_message=message,
                )
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2000]
            await repo.transition(
                ws,
                to=WorkspaceStatus.failed,
                reason_code=WORKTREE_MISSING_REASON_CODE,
            )
            await session.commit()
            return False

    def _defaults_for(self, agent: AgentRuntime) -> AgentDefaults | None:
        defaults = defaults_with_model_overrides(
            self._config.default_models,
            base=self._config.agent_defaults,
        )
        return defaults.get(agent)

    async def _run_baseline_coverage_preflight(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        profile: WorkspaceProfile,
    ) -> ValidationCoverageResult | None:
        """Measure coverage before agent edits so fix prompts know the baseline.

        This is intentionally non-blocking. A repository may already be below a
        newly tightened target, and AWF should still let explicitly launched
        coverage-improvement work proceed. The result is attached to later fix
        prompts and validation metadata so agents are steered toward adding
        tests instead of weakening the gate.
        """
        coverage = profile.validation.coverage
        if coverage.command is None and coverage.minimum_percent <= 0:
            return None
        result = await self._validation.run_profile_coverage(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            profile=profile,
            phase="baseline_coverage",
        )
        if result is not None and not result.ok:
            _log.info(
                "executor.baseline_coverage_below_policy",
                workspace_id=workspace_id,
                percent=result.percent,
                minimum_percent=result.minimum_percent,
                reason_code=result.reason_code,
            )
        return result

    async def _run_agent_task_with_optional_planning(
        self,
        *,
        adapter: AgentAdapter,
        workspace: Workspace,
        profile: WorkspaceProfile,
        compose_project: str,
        compose_file: Path,
        worktree_path: Path,
        model: str | None,
    ) -> str | None:
        planning = profile.planning
        if not planning.required:
            await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=workspace.task_prompt,
                model=model,
                workspace_id=workspace.id,
            )
            return None

        try:
            plan_path = render_workspace_path(planning.plan_path, workspace_id=workspace.id)
            report_path = render_workspace_path(
                planning.conformance_report_path,
                workspace_id=workspace.id,
            )
        except ValueError as exc:
            return f"planning profile is invalid: {exc}"

        before_plan = await self._changed_paths(worktree_path)
        baseline_sha: str | None = None
        rev_r = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "HEAD",
            ]
        )
        if rev_r.ok and rev_r.stdout.strip():
            baseline_sha = rev_r.stdout.strip()
        await adapter.run(
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_planning_prompt(
                task_prompt=workspace.task_prompt,
                plan_path=plan_path,
            ),
            model=model,
            workspace_id=workspace.id,
        )
        dirty_paths = await self._changed_paths(worktree_path)
        committed_paths = (
            await self._committed_paths_since(worktree_path, baseline_sha)
            if baseline_sha is not None
            else set()
        )
        after_plan = dirty_paths | committed_paths
        if plan_path not in after_plan:
            return f"planning phase did not create or modify required plan file `{plan_path}`"
        if planning.enforce_plan_only_changes:
            extra = sorted(after_plan - before_plan - {plan_path})
            if extra:
                changed = ", ".join(path.as_posix() for path in extra[:10])
                return f"planning phase changed files outside `{plan_path}`: {changed}"

        gaps: tuple[str, ...] = ()
        last_report: PlanConformanceReport | None = None
        for iteration in range(planning.max_iterations + 1):
            await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=build_execution_prompt(
                    task_prompt=workspace.task_prompt,
                    plan_path=plan_path,
                    iteration=iteration,
                    gaps=gaps,
                ),
                model=model,
                workspace_id=workspace.id,
            )
            before_compare = await self._changed_paths(worktree_path)
            compare_result = await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=build_conformance_prompt(
                    task_prompt=workspace.task_prompt,
                    plan_path=plan_path,
                    report_path=report_path,
                    iteration=iteration,
                ),
                model=model,
                workspace_id=workspace.id,
            )
            after_compare = await self._changed_paths(worktree_path)
            if planning.fail_on_unexplained_deviation:
                extra = sorted(after_compare - before_compare - {report_path})
                if extra:
                    changed = ", ".join(path.as_posix() for path in extra[:10])
                    return f"conformance phase changed files outside `{report_path}`: {changed}"

            report_text = (
                _read_text_if_present(worktree_path / report_path) or compare_result.stdout
            )
            report = parse_conformance_report(report_text)
            last_report = report
            if report.satisfied:
                _log.info(
                    "executor.planning_conformance_satisfied",
                    workspace_id=workspace.id,
                    iteration=iteration,
                    summary=report.summary,
                )
                return None
            gaps = report.gaps or (report.summary,)
            _log.info(
                "executor.planning_conformance_needs_iteration",
                workspace_id=workspace.id,
                iteration=iteration,
                max_iterations=planning.max_iterations,
                gaps=list(gaps),
                reason_code=report.reason_code,
            )

        if last_report is None:  # pragma: no cover - defensive
            return "planning conformance did not run"
        gap_text = "; ".join(last_report.gaps) or last_report.summary
        return (
            "plan conformance was not satisfied after "
            f"{planning.max_iterations} iteration(s): {gap_text}"
        )

    async def _changed_paths(self, worktree_path: Path) -> set[Path]:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        )
        if not result.ok:
            raise RuntimeError(
                f"git status failed while checking workspace changes: {result.stderr}"
            )
        return changed_paths_from_porcelain(result.stdout)

    async def _committed_paths_since(self, worktree_path: Path, since: str) -> set[Path]:
        result = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "diff",
                "--name-only",
                f"{since}..HEAD",
            ]
        )
        if not result.ok:
            raise RuntimeError(
                f"git diff --name-only failed while checking committed paths: {result.stderr}"
            )
        return {Path(line.strip()) for line in result.stdout.splitlines() if line.strip()}

    async def _claim_ready(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> Workspace | None:
        """Atomically transition a ready workspace to running before execution."""
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                workspace_id,
                from_status=WorkspaceStatus.ready,
                to=WorkspaceStatus.running,
                reason_code="EXECUTOR_CLAIMED",
            )
            if ws is not None:
                ws.execution_claimed_by = execution_owner_id
                ws.execution_claim_expires_at = execution_lease_expires_at
                await session.commit()
                return ws

            current = await repo.get(workspace_id)
            if current is None:
                _log.warning("executor.skip_unknown", workspace_id=workspace_id)
                return None
            _log.info(
                "executor.skip_not_ready",
                workspace_id=workspace_id,
                status=current.status,
            )
            return None

    async def _recheck_status(
        self,
        workspace_id: str,
        *,
        expected: WorkspaceStatus,
        action: str,
        reason_code: str = "EXECUTOR_STALE_STATUS",
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                _log.warning(
                    "executor.skip_unknown",
                    workspace_id=workspace_id,
                    action=action,
                )
                return False
            if ws.status == expected.value:
                return True
            await self._record_stale_action_skip(
                repo,
                ws,
                action=action,
                expected=expected,
                reason_code=reason_code,
            )
            await session.commit()
            return False

    async def _transition_if_current(
        self,
        workspace_id: str,
        *,
        from_status: WorkspaceStatus,
        to: WorkspaceStatus,
        reason: str,
        action: str,
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover - destroyed mid-flight
                return False
            if ws.status != from_status.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action=action,
                    expected=from_status,
                    reason_code="EXECUTOR_STALE_STATUS",
                )
                await session.commit()
                return False
            await repo.transition(ws, to=to, reason_code=reason)
            await session.commit()
            return True

    async def _record_stale_action_skip(
        self,
        repo: WorkspaceRepository,
        ws: Workspace,
        *,
        action: str,
        expected: WorkspaceStatus,
        reason_code: str,
    ) -> None:
        _log.info(
            "executor.skip_stale_status",
            workspace_id=ws.id,
            action=action,
            expected_status=expected.value,
            status=ws.status,
        )
        await repo.add_event(
            ws,
            event_type="workspace.stale_action_skipped",
            reason_code=reason_code,
            payload={
                "action": action,
                "expected_status": expected.value,
                "actual_status": ws.status,
            },
        )

    async def _mark_failed(
        self,
        *,
        workspace_id: str,
        from_status: WorkspaceStatus,
        failure_reason: FailureReason,
        message: str,
        reason_code: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover
                return
            if ws.status != from_status.value:
                # Already moved (e.g. cancelled) — respect it.
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="mark_failed",
                    expected=from_status,
                    reason_code="EXECUTOR_MARK_FAILED_SKIPPED",
                )
                await session.commit()
                return
            ws.failure_reason = failure_reason.value
            ws.failure_message = message
            if reason_code == EXEC_PROCESS_CLEANUP_FAILED:
                await repo.add_event(
                    ws,
                    event_type="workspace.exec_process_cleanup_failed",
                    reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                    payload={"message": message[:1000]},
                )
            await repo.transition(
                ws,
                to=WorkspaceStatus.failed,
                reason_code=reason_code or failure_reason.value.upper(),
            )
            await session.commit()

    async def _start_validation_run(
        self,
        *,
        workspace_id: str,
        profile: WorkspaceProfile,
        base_commit: str | None,
        workspace_head_sha: str | None,
        target_branch: str | None,
        target_head_sha: str | None,
        tier: int,
    ) -> str:
        command_records = _validation_run_command_records(
            profile=profile,
            phase_names=("post_agent", "validate"),
            run_healthchecks=True,
        )
        async with self._session_factory() as session:
            attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
            run = await ValidationRunRepository(session).start(
                workspace_id=workspace_id,
                attempt_id=attempt.id if attempt is not None else None,
                tier=tier,
                commands=command_records,
                base_commit=base_commit,
                base_sha=base_commit,
                workspace_head_sha=workspace_head_sha,
                target_branch=target_branch,
                target_head_sha=target_head_sha,
                profile_name=profile.name,
                profile_version=profile.version,
                profile_source=profile.source,
                resolved_profile_digest=resolved_profile_digest(profile),
                environment_identity_digest=environment_identity_digest(profile),
                environment_identity_inputs=environment_identity_inputs(profile),
                log_stream_refs=_validation_run_log_stream_refs(command_records),
                started_at=datetime.now(UTC),
            )
            await session.commit()
            return run.id

    async def _capture_workspace_head_sha(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
    ) -> str | None:
        result = await self._runner.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        )
        head_sha = result.stdout.strip()
        if result.ok and head_sha:
            return head_sha
        _log.warning(
            "executor.validation_workspace_head_sha_capture_failed",
            workspace_id=workspace_id,
            returncode=result.returncode,
            stderr=result.stderr[:400],
        )
        return None

    async def _begin_rebase_recovery_operation(
        self,
        *,
        workspace_id: str,
        base_branch: str,
        remote_branch: str,
        reason: str,
        reason_code: str,
        source_base_sha: str | None,
        source_head_sha: str | None,
        recovery_payload: Mapping[str, Any],
    ) -> MonitorOperationHandle | None:
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-recovery
                return None
            pr_number = _int_or_none(recovery_payload.get("pr_number")) or workspace.pr_number
            if pr_number is None:
                pr_number = 0
            payload = build_monitor_operation_payload(
                workspace=workspace,
                action="rebase_only",
                requested_action="rebase",
                reason=reason,
                reason_code=reason_code,
                pr_number=pr_number,
                source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
                source_base_sha=source_base_sha or workspace.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                recovery_mode="rebase_only",
            )
            handle = await create_or_start_monitor_operation(
                session,
                workspace_id=workspace_id,
                operation_type=OperationType.rebase,
                payload=payload,
                idempotency_key=monitor_operation_idempotency_key(
                    workspace_id=workspace_id,
                    action="rebase_only",
                    pr_number=pr_number,
                    reason_code=reason_code,
                    source_head_sha=source_head_sha or workspace.monitor_last_commit_sha,
                    source_base_sha=source_base_sha or workspace.base_commit,
                ),
                status=OperationStatus.running,
            )
            await session.commit()
            return handle

    async def _finish_rebase_recovery_operation(
        self,
        operation: MonitorOperationHandle | None,
        *,
        status: OperationStatus,
        result: Mapping[str, Any],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if operation is None or not operation.should_finish:
            return
        async with self._session_factory() as session:
            await finish_monitor_operation(
                session,
                operation_id=operation.operation_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            await session.commit()

    async def _run_monitor_rebase_recovery(
        self,
        *,
        workspace_id: str,
        worktree_path: Path,
        base_branch: str,
        branch_name: str,
        remote_branch: str,
        reason: str,
        recovery_payload: Mapping[str, Any],
    ) -> _RebaseRecoveryResult:
        """Rebase an already-open PR branch onto the latest target branch.

        The PR monitor dispatches ``recovery_mode='rebase_only'`` when a
        merge candidate is stale because the target branch moved. Older
        executor code treated that as validation-only, which left the same
        stale reason active and caused an infinite
        ``monitoring_pr -> ready -> running -> validating`` loop. This
        recovery performs the real branch update once, pushes it, records a
        rebase operation, and lets the normal Tier 2 validation pass prove the
        rebased branch.
        """

        async def git(args: list[str]) -> CommandResult:
            return await self._runner.run(
                [
                    "git",
                    *git_safe_directory_config_args(worktree_path),
                    "-C",
                    str(worktree_path),
                    *args,
                ]
            )

        source_base_sha = _str_or_none(recovery_payload.get("source_base_sha"))
        source_head_sha = _str_or_none(recovery_payload.get("source_head_sha"))
        operation = await self._begin_rebase_recovery_operation(
            workspace_id=workspace_id,
            base_branch=base_branch,
            remote_branch=remote_branch,
            reason=reason,
            reason_code=_str_or_none(recovery_payload.get("reason_code"))
            or "MONITOR_REBASE_RECOVERY",
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
            recovery_payload=recovery_payload,
        )
        try:
            fetch = await git(["fetch", "origin", base_branch])
            if not fetch.ok:
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git fetch origin {base_branch} failed: {fetch.stderr}"
                )

            switch = await git(["switch", branch_name])
            if not switch.ok:
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git switch {branch_name} failed: {switch.stderr}"
                )

            target_ref = f"origin/{base_branch}"
            already_contains_target = await git(["merge-base", "--is-ancestor", target_ref, "HEAD"])
            if already_contains_target.ok:
                return await self._record_current_rebase_recovery_head(
                    git=git,
                    workspace_id=workspace_id,
                    target_ref=target_ref,
                    operation=operation,
                    source_base_sha=source_base_sha,
                    source_head_sha=source_head_sha,
                    rebased=False,
                    pushed=False,
                )
            if already_contains_target.returncode not in {1}:
                raise _MonitorRebaseRecoveryError(
                    "rebase recovery: git merge-base --is-ancestor "
                    f"{target_ref} HEAD failed: {already_contains_target.stderr}"
                )

            rebase = await git(["rebase", target_ref])
            if not rebase.ok:
                await git(["rebase", "--abort"])
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git rebase {target_ref} failed: {rebase.stderr}"
                )

            return await self._record_current_rebase_recovery_head(
                git=git,
                workspace_id=workspace_id,
                target_ref=target_ref,
                remote_branch=remote_branch,
                operation=operation,
                source_base_sha=source_base_sha,
                source_head_sha=source_head_sha,
                rebased=True,
                pushed=True,
            )
        except Exception as exc:
            await self._finish_rebase_recovery_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": "MONITOR_RECOVERY_REBASE_FAILED",
                    "source_base_sha": source_base_sha,
                    "source_head_sha": source_head_sha,
                },
                error_code="MONITOR_RECOVERY_REBASE_FAILED",
                error_message=str(exc),
            )
            raise

    async def _record_current_rebase_recovery_head(
        self,
        *,
        git: Callable[[list[str]], Awaitable[CommandResult]],
        workspace_id: str,
        target_ref: str,
        remote_branch: str | None = None,
        operation: MonitorOperationHandle | None,
        source_base_sha: str | None,
        source_head_sha: str | None,
        rebased: bool,
        pushed: bool,
    ) -> _RebaseRecoveryResult:
        """Record the current branch head after rebase-style recovery.

        A monitor may dispatch rebase recovery after GitHub has already
        synced the PR branch with the target branch. In that case the
        branch already contains ``origin/<base>`` and running ``git rebase``
        again can fail while replaying commits from a merge-synced branch.
        Treating the already-synced state as a successful refresh keeps the
        recovery path idempotent; Tier 2 validation still proves the branch
        before merge eligibility is restored.
        """

        base_sha_result = await git(["rev-parse", target_ref])
        if not base_sha_result.ok or not base_sha_result.stdout.strip():
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: could not resolve {target_ref}: {base_sha_result.stderr}"
            )
        base_sha = base_sha_result.stdout.strip()

        head_sha_result = await git(["rev-parse", "HEAD"])
        if not head_sha_result.ok or not head_sha_result.stdout.strip():
            raise _MonitorRebaseRecoveryError(
                f"rebase recovery: could not resolve HEAD: {head_sha_result.stderr}"
            )
        head_sha = head_sha_result.stdout.strip()

        if remote_branch is not None:
            push = await git(["push", "--force-with-lease", "origin", f"HEAD:{remote_branch}"])
            if not push.ok:
                raise _MonitorRebaseRecoveryError(
                    f"rebase recovery: git push --force-with-lease failed: {push.stderr}"
                )

        await self._record_rebase_recovery_success(
            workspace_id=workspace_id,
            base_sha=base_sha,
            head_sha=head_sha,
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
            operation=operation,
            pushed=pushed,
            rebased=rebased,
        )
        return _RebaseRecoveryResult(base_sha=base_sha, head_sha=head_sha)

    async def _record_rebase_recovery_success(
        self,
        *,
        workspace_id: str,
        base_sha: str,
        head_sha: str,
        source_base_sha: str | None,
        source_head_sha: str | None,
        operation: MonitorOperationHandle | None,
        pushed: bool,
        rebased: bool,
    ) -> None:
        async with self._session_factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            if workspace is None:  # pragma: no cover - destroyed mid-recovery
                return
            workspace.base_commit = base_sha
            workspace.monitor_last_commit_sha = head_sha

            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is not None:
                candidate.base_sha = base_sha
                candidate.head_sha = head_sha
                candidate.workspace.base_commit = base_sha
                candidate.workspace.monitor_last_commit_sha = head_sha
                sync_candidate_readiness(
                    candidate,
                    workspace=candidate.workspace,
                    attempt=candidate.attempt,
                    sync_validation_staleness=False,
                )

            if operation is not None and operation.should_finish:
                await finish_monitor_operation(
                    session,
                    operation_id=operation.operation_id,
                    status=OperationStatus.succeeded,
                    result={
                        "status": "succeeded",
                        "reason_code": "REBASE_OK",
                        "source_base_sha": source_base_sha,
                        "source_head_sha": source_head_sha,
                        "target_base_sha": base_sha,
                        "target_head_sha": head_sha,
                        "pushed": pushed,
                        "rebased": rebased,
                    },
                )
            await session.commit()

    async def _clear_rebase_recovery_staleness(
        self,
        *,
        workspace_id: str,
    ) -> None:
        async with self._session_factory() as session:
            candidate = await MergeCandidateRepository(
                session
            ).get_open_for_workspace_with_merge_inputs(workspace_id)
            if candidate is None:
                return
            await StaleReasonRepository(session).replace_active_findings(
                workspace_id=candidate.workspace_id,
                candidate_id=candidate.id,
                attempt_id=candidate.attempt_id,
                task_id=candidate.task_id,
                findings=[],
            )
            candidate.stale = False
            candidate.stale_reason = None
            sync_candidate_readiness(
                candidate,
                workspace=candidate.workspace,
                attempt=candidate.attempt,
                sync_validation_staleness=False,
            )
            await session.commit()

    async def _start_pending_recovery_operations(
        self,
        *,
        workspace_id: str,
    ) -> None:
        """Flush pending validate-only recovery operations to ``running``.

        Recovery dispatch creates the validate Operation in ``pending``;
        without an explicit transition the row would jump straight to
        ``succeeded``/``failed`` with
        ``started_at == finished_at``, which loses the recovery
        lifecycle for observability tooling.
        """
        async with self._session_factory() as session:
            repo = OperationRepository(session)
            pending = await repo.list_for_workspace(
                workspace_id,
                status=OperationStatus.pending,
                limit=100,
            )
            for operation in pending:
                if not _is_validate_only_recovery_payload(operation.payload):
                    continue
                await repo.start(operation)
            await session.commit()

    async def _finish_active_recovery_operations(
        self,
        *,
        workspace_id: str,
        status: OperationStatus,
        reason_code: str | None,
        error_message: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = OperationRepository(session)
            pending = await repo.list_for_workspace(
                workspace_id,
                status=OperationStatus.pending,
                limit=100,
            )
            running = await repo.list_for_workspace(
                workspace_id,
                status=OperationStatus.running,
                limit=100,
            )
            result = {"reason_code": reason_code}
            for operation in [*pending, *running]:
                if not _is_validate_only_recovery_payload(operation.payload):
                    continue
                await repo.finish(
                    operation,
                    status=status,
                    result=result,
                    error_code=reason_code if status == OperationStatus.failed else None,
                    error_message=error_message,
                )
            await session.commit()

    async def _finish_pending_validate_operations(
        self,
        *,
        workspace_id: str,
        status: OperationStatus,
        validation_run_id: str,
        requested_tier: int,
        reason_code: str | None,
        error_message: str | None = None,
        coverage: dict[str, object] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await self._finish_pending_validate_operations_in_session(
                session,
                workspace_id=workspace_id,
                status=status,
                validation_run_id=validation_run_id,
                requested_tier=requested_tier,
                reason_code=reason_code,
                error_message=error_message,
                coverage=coverage,
            )
            await session.commit()

    async def _finish_pending_validate_operations_in_session(
        self,
        session: AsyncSession,
        *,
        workspace_id: str,
        status: OperationStatus,
        validation_run_id: str,
        requested_tier: int,
        reason_code: str | None,
        error_message: str | None = None,
        coverage: dict[str, object] | None = None,
    ) -> None:
        repo = OperationRepository(session)
        pending = await repo.list_for_workspace(
            workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            limit=100,
        )
        running = await repo.list_for_workspace(
            workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
            limit=100,
        )
        result = {
            "validation_run_id": validation_run_id,
            "requested_tier": requested_tier,
            "reason_code": reason_code,
        }
        validation_run = await ValidationRunRepository(session).get(validation_run_id)
        if validation_run is not None and isinstance(validation_run.log_stream_refs, dict):
            result["log_stream_refs"] = dict(validation_run.log_stream_refs)
        if coverage is not None:
            result["coverage"] = coverage
        for operation in [*pending, *running]:
            payload = dict(operation.payload or {})
            payload.setdefault("requested_tier", requested_tier)
            operation.payload = payload
            await repo.finish(
                operation,
                status=status,
                result=result,
                error_code=reason_code if status == OperationStatus.failed else None,
                error_message=error_message,
            )

    async def _finish_validation_run(
        self,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        retry_count: int = 0,
        coverage: dict[str, object] | None = None,
        command_retries: list[int] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await ValidationRunRepository(session).finish(
                validation_run_id,
                status=status,
                reason_code=reason_code,
                finished_at=datetime.now(UTC),
                retry_count=retry_count,
                coverage=coverage,
                command_retries=command_retries,
            )
            await session.commit()

    async def _set_validation_run_target_head_sha(
        self,
        *,
        validation_run_id: str,
        target_head_sha: str,
        workspace_head_sha: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await ValidationRunRepository(session).update_target_head_sha(
                validation_run_id,
                target_head_sha=target_head_sha,
                workspace_head_sha=workspace_head_sha,
            )
            await session.commit()


_PR_NUMBER_RE = re.compile(r"/pull/(\d+)(?:/|$)")


def _extract_pr_number(pr_url: str) -> int | None:
    """Parse the PR number from a GitHub PR URL.

    Matches both the canonical ``https://github.com/<owner>/<repo>/pull/123``
    and the trailing-slash variant. Returns ``None`` if the URL doesn't look
    like a PR URL — the monitor then simply won't run (executor logs a
    warning via the transition to ``monitoring_pr`` still succeeding; the
    monitor itself asserts on pr_number and terminates with a clear failure).
    """
    match = _PR_NUMBER_RE.search(pr_url)
    return int(match.group(1)) if match else None


def _worktree_missing_message(worktree_path: Path, action: str) -> str:
    return (
        f"{WORKTREE_MISSING_REASON_CODE}: managed worktree path is missing or not a "
        f"directory while preparing `{action}`: {worktree_path}"
    )


def _missing_monitor_recovery_metadata(ws: Workspace) -> list[str]:
    missing: list[str] = []
    if ws.pr_number is None:
        missing.append("pr_number")
    if not ws.pr_url:
        missing.append("pr_url")
    if not ws.remote_push_branch:
        missing.append(
            f"remote_push_branch (task_kind={ws.task_kind}, branch_name={ws.branch_name!r})"
        )
    if not ws.compose_project_name:
        missing.append("compose_project_name")
    if not ws.compose_file_path:
        missing.append("compose_file_path")
    return missing


def _call_pr_monitor_factory(
    factory: Callable[..., _MonitorRunnerProto],
    *,
    adapter: AgentAdapter,
    profile: WorkspaceProfile,
    workspace: Workspace,
) -> _MonitorRunnerProto:
    """Call a monitor factory with the richest supported context.

    Production factories may need the persisted workspace row for monitor
    policy. Older tests and scripts predate profile/workspace-aware execution
    and expose one- or two-argument factories. We inspect arity before the call
    so a ``TypeError`` raised inside the factory body is never mistaken for an
    argument-count mismatch.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        # Without a signature, preserve the historical two-argument fallback;
        # probing by calling would risk masking TypeErrors raised inside the
        # factory body.
        return factory(adapter, profile)

    bind_errors: list[TypeError] = []
    for args in ((adapter, profile, workspace), (adapter, profile), (adapter,)):
        try:
            signature.bind(*args)
        except TypeError as exc:
            bind_errors.append(exc)
            continue
        return factory(*args)

    raise bind_errors[0]


def _build_pr_body(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    """Standard PR description generated from the workspace's task metadata."""
    external_id = f"\n**External task ID**: {ws.task_external_id}" if ws.task_external_id else ""
    return (
        f"Automatically opened by AWF workspace `{ws.id}` "
        f"({_agent_pr_identity(ws, defaults=defaults)}).\n"
        f"{external_id}\n\n"
        f"### Task\n{ws.task_prompt}\n\n"
        f"---\nValidation: "
        f"{_validation_command_count(ws)} profile command(s) passed inside the workspace container.\n"
    )


def _agent_pr_identity(ws: Workspace, *, defaults: AgentDefaults | None = None) -> str:
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    model = _nonblank_policy_string(policy, "agent_model") or (defaults.model if defaults else None)
    effort = _nonblank_policy_string(policy, "agent_effort") or (defaults.effort if defaults else None)

    parts = [f"agent: `{ws.agent}`"]
    if model is not None:
        parts.append(f"model: `{model}`")
    if effort is not None:
        parts.append(f"effort: `{effort}`")
    return ", ".join(parts)


def _nonblank_policy_string(policy: Mapping[str, Any], key: str) -> str | None:
    value = policy.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _profile_for_workspace(ws: Workspace, *, worktree_path: Path) -> WorkspaceProfile:
    if ws.resolved_profile:
        return WorkspaceProfile.model_validate(ws.resolved_profile)
    return resolve_workspace_profile(
        worktree_path=worktree_path,
        inline_profile=ws.requested_profile,
        profile_ref=ws.profile_ref or ws.env_profile or "auto",
        validation_commands=list(ws.test_commands),
    ).profile


def _agent_model_for_workspace(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> str | None:
    workspace_defaults = _agent_defaults_for_workspace(ws, defaults)
    return workspace_defaults.model if workspace_defaults is not None else None


def _agent_defaults_for_workspace(
    ws: Workspace,
    defaults: AgentDefaults | None,
) -> AgentDefaults | None:
    """Return adapter defaults after applying workspace-persisted policy.

    Agent adapters are handed to the PR monitor, which invokes recovery
    prompts without passing an explicit ``model`` each time. Binding the
    workspace's effective model into the adapter is therefore important:
    an opencode workspace launched with ``ollama/glm-5.1:cloud`` must not
    drift back to AWF's opencode default (currently Kimi) while resolving
    PR comments.
    """
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    model = _nonblank_policy_string(policy, "agent_model")
    effort = _nonblank_policy_string(policy, "agent_effort")
    if model is None and effort is None:
        return defaults
    if defaults is not None:
        return replace(
            defaults,
            model=model or defaults.model,
            effort=effort or defaults.effort,
        )
    if model is None:
        return None
    return AgentDefaults(model=model, effort=effort)


def _failure_reason_for_phase(first_fail: object | None) -> FailureReason:
    phase = getattr(first_fail, "phase", None)
    reason_code = getattr(first_fail, "reason_code", None)
    if phase == "healthcheck":
        return FailureReason.health_check_failure
    if reason_code == "PHASE_TIMEOUT":
        return FailureReason.phase_timeout
    if phase in {"setup", "pre_agent"}:
        return FailureReason.service_startup_failure
    return FailureReason.validation_failure


def _validation_command_count(ws: Workspace) -> int:
    if ws.resolved_profile:
        profile = WorkspaceProfile.model_validate(ws.resolved_profile)
        coverage_count = 1 if profile.validation.coverage.command is not None else 0
        return (
            len(profile.phases.post_agent) + len(profile.phases.validate_commands) + coverage_count
        )
    return len(ws.test_commands)


def _read_text_if_present(path: Path) -> str | None:
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def _validation_run_command_records(
    *,
    profile: WorkspaceProfile,
    phase_names: tuple[str, ...],
    run_healthchecks: bool,
) -> list[dict[str, Any]]:
    ordered: list[tuple[str, str]] = []
    if run_healthchecks:
        ordered.extend(
            ("healthcheck", healthcheck.command) for healthcheck in profile.validation.healthchecks
        )
    ordered.extend(
        (phase, command.command) for phase, command in profile.phases.commands_for(phase_names)
    )
    if "validate" in phase_names and profile.validation.coverage.command is not None:
        ordered.append(("coverage", profile.validation.coverage.command.command))

    records: list[dict[str, Any]] = []
    phase_indices: dict[str, int] = {}
    for phase, command in ordered:
        phase_indices[phase] = phase_indices.get(phase, 0) + 1
        command_index = phase_indices[phase]
        label = f"{command_index:02d}_{phase}"
        records.append(
            {
                "phase": phase,
                "command_index": command_index,
                "command": command,
                "stream_ids": {
                    "stdout": f"validation.{label}.stdout",
                    "stderr": f"validation.{label}.stderr",
                },
            }
        )
    return records


def _validation_tier_for_workspace(workspace: Workspace, profile: WorkspaceProfile) -> int:
    profile_tier = profile.validation.requested_tier
    task_class_tier = 1
    if workspace.task_class == TaskClass.migration_task.value:
        task_class_tier = 3
    elif workspace.task_class in {
        TaskClass.refactor_task.value,
        TaskClass.dependency_task.value,
        TaskClass.build_config_task.value,
    }:
        task_class_tier = 2
    return max(profile_tier, task_class_tier)


def _validation_run_log_stream_refs(
    command_records: list[dict[str, Any]],
) -> dict[str, list[dict[str, str | None]]]:
    refs: list[dict[str, str | None]] = []
    for command in command_records:
        stream_ids = command.get("stream_ids")
        if not isinstance(stream_ids, dict):
            refs.append({"stdout": None, "stderr": None})
            continue
        stdout = stream_ids.get("stdout")
        stderr = stream_ids.get("stderr")
        refs.append(
            {
                "stdout": stdout if isinstance(stdout, str) else None,
                "stderr": stderr if isinstance(stderr, str) else None,
            }
        )
    return {"commands": refs}


def _validation_run_reason_code(result: ValidationResult) -> str:
    if result.all_passed:
        return "VALIDATION_OK"
    if result.coverage is not None and not result.coverage.ok:
        return result.coverage.reason_code
    first_failure = result.first_failure
    if first_failure is None:
        return "VALIDATION_FAILED"
    return first_failure.reason_code


def _apply_baseline_coverage_ratchet(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> ValidationResult:
    """Accept coverage baseline debt only when a workspace does not regress it.

    AWF's self profile carries an aspirational 99% coverage target. Until the
    existing repo baseline reaches that target, unrelated feature PRs should
    not be forced to repay the whole historical debt. They must, however,
    preserve or improve the measured baseline and must not lower the gate.
    """
    coverage = result.coverage
    if not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=baseline_coverage,
    ):
        return result

    assert coverage is not None  # narrowed by helper above
    command_result = coverage.command_result
    adjusted_command = (
        replace(
            command_result,
            returncode=0,
            reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
            policy_failed=False,
        )
        if command_result is not None
        else None
    )
    adjusted_coverage = replace(
        coverage,
        status="baseline_debt",
        reason_code="COVERAGE_BASELINE_DEBT_NO_REGRESSION",
        command_result=adjusted_command,
    )
    adjusted_commands = list(result.commands)
    if adjusted_command is not None and command_result is not None:
        adjusted_commands = [
            adjusted_command
            if command.stdout_path == command_result.stdout_path
            and command.stderr_path == command_result.stderr_path
            else command
            for command in result.commands
        ]
    return replace(result, commands=adjusted_commands, coverage=adjusted_coverage)


def _coverage_preserves_below_threshold_baseline(
    coverage: ValidationCoverageResult | None,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> bool:
    if coverage is None or baseline_coverage is None:
        return False
    if coverage.reason_code != "COVERAGE_BELOW_THRESHOLD":
        return False
    if coverage.percent is None or baseline_coverage.percent is None:
        return False
    if baseline_coverage.percent >= coverage.minimum_percent:
        return False
    return coverage.percent + 0.005 >= baseline_coverage.percent


def _validation_run_coverage_metadata(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> dict[str, object] | None:
    if result.coverage is None:
        return None
    metadata = result.coverage.as_metadata()
    if baseline_coverage is not None:
        metadata["baseline_percent"] = (
            float(baseline_coverage.percent)
            if baseline_coverage.percent is not None
            else None
        )
        metadata["baseline_status"] = baseline_coverage.status
        metadata["baseline_reason_code"] = baseline_coverage.reason_code
    return metadata


def _format_coverage_gaps(gaps: list[dict[str, object]]) -> str:
    if not gaps:
        return ""
    top = gaps[:5]
    lines = ["top uncovered areas:"]
    for g in top:
        file_name = g.get("file", "")
        missing = g.get("missing_lines", [])
        missing_cast = missing if isinstance(missing, list) else []
        missing_str = ", ".join(str(m) for m in missing_cast) if missing_cast else "(no missing lines)"
        lines.append(f"  {file_name}: {missing_str}")
    return "\n".join(lines)


def _validation_failure_message(
    result: ValidationResult,
    *,
    baseline_coverage: ValidationCoverageResult | None = None,
) -> str:
    coverage = result.coverage
    if coverage is not None and not coverage.ok:
        baseline_debt = (
            baseline_coverage is not None
            and baseline_coverage.percent is not None
            and baseline_coverage.percent < coverage.minimum_percent
        )
        baseline_suffix = (
            f"; pre-agent base coverage was {baseline_coverage.percent:.1f}%"
            f" against the same {coverage.minimum_percent:.1f}% requirement"
            if baseline_debt and baseline_coverage is not None and baseline_coverage.percent is not None
            else ""
        )
        if coverage.reason_code == "COVERAGE_BELOW_THRESHOLD" and coverage.percent is not None:
            gap_lines = _format_coverage_gaps(coverage.gaps if coverage.gaps else [])
            gap_text = f"\n{gap_lines}" if gap_lines else ""
            return (
                "validation failed: coverage "
                f"{coverage.percent:.1f}% is below required {coverage.minimum_percent:.1f}%"
                f"{baseline_suffix}; add meaningful tests and do not lower coverage thresholds"
                f"{gap_text}"
            )
        if coverage.reason_code == "COVERAGE_NOT_FOUND":
            return "validation failed: coverage output was not found"
        if coverage.reason_code == "COVERAGE_COMMAND_FAILED":
            return (
                "validation failed: coverage command failed"
                f"{baseline_suffix}; fix the failing tests or add meaningful coverage, "
                "do not lower coverage thresholds"
            )
        if coverage.reason_code == "COVERAGE_PROVIDER_UNSUPPORTED":
            return f"validation failed: unsupported coverage provider {coverage.provider}"

    first_fail = result.first_failure
    return f"validation failed: {first_fail.command}" if first_fail else "validation failed"


def _git_name_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]
