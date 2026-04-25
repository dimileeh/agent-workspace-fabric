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

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentDefaults, AgentRunError, get_adapter
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS, defaults_with_model_overrides
from awf.common.commands import AsyncCommandRunner
from awf.common.logging import get_logger
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeManager
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.logs import LogStore
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError
from awf.runtime.validation import ValidationRunner


class _MonitorRunnerProto(Protocol):
    """Minimum surface the executor needs from a PR monitor runner.

    Declared as a Protocol so the executor doesn't structurally depend
    on ``PullRequestMonitorRunner`` — tests can pass a tiny stub, and
    the monitor stage is a clean extension seam for Phase 2 variants
    (merge queue, release-PR monitor, etc.)."""

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None: ...


_log = get_logger(__name__)


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

    async def execute(self, workspace_id: str) -> None:
        """Drive a ``ready`` workspace to ``completed`` (or ``failed``).

        The function is idempotent in the sense that it refuses to run on a
        workspace that is not currently in ``ready`` — useful when a poll
        loop races with a manual invocation.
        """
        ws = await self._claim_ready(workspace_id)
        if ws is None:
            return

        compose_file = (
            Path(ws.compose_file_path)
            if ws.compose_file_path
            else self._config.compose_projects_root / workspace_id / "compose.yml"
        )
        compose_project = ws.compose_project_name or f"awf_{workspace_id}"
        worktree_path = self._config.worktrees_root / workspace_id

        # ── Step 1: agent CLI runs the task inside the container ────────────
        try:
            agent = AgentRuntime(ws.agent)
            defaults = self._defaults_for(agent)
            default_model = defaults.model if defaults else None
            adapter = get_adapter(
                agent,
                runner=self._runner,
                defaults=defaults,
                log_store=self._log_store,
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
            await adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=ws.task_prompt,
                model=default_model,
                workspace_id=workspace_id,
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
            return await self._runner.run(["git", "-C", str(worktree_host), *args])

        expected_branch = ws.branch_name or f"awf/{workspace_id}"

        try:
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
        await self._transition(workspace_id, to=WorkspaceStatus.validating, reason="AGENT_RUN_OK")

        max_fix_passes = self._config.max_validation_fix_passes
        profile = _profile_for_workspace(ws, worktree_path=worktree_path)
        validation_commands = [
            command.command
            for _, command in profile.phases.commands_for(("post_agent", "validate"))
        ]
        test_commands_tuple = tuple(validation_commands)
        last_failure_message: str | None = None
        for pass_number in range(max_fix_passes + 1):
            # pass_number == 0 is the initial run (already-committed agent
            # work). 1..N are fix attempts driven by the retry prompt.
            val_result = await self._validation.run_profile_phases(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                profile=profile,
                phase_names=("post_agent", "validate"),
                run_healthchecks=True,
            )
            if val_result.all_passed:
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
            last_failure_message = (
                f"validation failed: {first_fail.command}" if first_fail else "validation failed"
            )

            if pass_number >= max_fix_passes or first_fail is None:
                # Exhausted our budget (or no failure details to anchor a
                # fix prompt on) — mark failed and let the operator triage.
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
            )
            fix_prompt = build_fix_prompt(fix_context)
            _log.info(
                "executor.fix_pass_start",
                workspace_id=workspace_id,
                pass_number=pass_number + 1,
                max_fix_passes=max_fix_passes,
                failed_command=first_fail.command,
            )
            try:
                await adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=fix_prompt,
                    model=default_model,
                    workspace_id=workspace_id,
                )
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
                )

            # Commit whatever the fix pass produced. Simpler than the
            # initial post-agent commit block — orphan-history recovery
            # isn't possible here (HEAD already descends from base after
            # the initial run succeeded); zero-change fix passes are
            # allowed (agent may think no change was needed, which next
            # validation will confirm or refute).
            fix_add = await _git_in_worktree(["add", "-A"])
            if not fix_add.ok:
                _log.warning(
                    "executor.fix_pass_add_failed",
                    workspace_id=workspace_id,
                    stderr=fix_add.stderr[:400],
                )
            fix_cached = await _git_in_worktree(["diff", "--cached", "--name-only"])
            if fix_cached.stdout.strip():
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
                        str(worktree_host),
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

        # ── Step 4: persist PR URL + (optionally) hand off to monitor ──────
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            persisted = await repo.get(workspace_id)
            if persisted is None:  # pragma: no cover - destroyed mid-flight
                return
            persisted.pr_url = pr.url
            persisted.pr_number = _extract_pr_number(pr.url)
            # Resolve which monitor (if any) to hand off to. Pre-constructed
            # ``pr_monitor`` wins (tests); otherwise the factory builds one
            # from the per-task adapter now that we have it.
            monitor: _MonitorRunnerProto | None = self._pr_monitor
            if monitor is None and self._pr_monitor_factory is not None:
                monitor = _call_pr_monitor_factory(
                    self._pr_monitor_factory,
                    adapter=adapter,
                    profile=profile,
                )

            if monitor is not None:
                # Hand off to the monitor — it will transition to completed
                # (on merge) or failed (on abort / cap / close).
                await repo.transition(
                    persisted, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED"
                )
                await session.commit()
            else:
                # No monitor wired (legacy executor path / unit-test shim) —
                # preserve the original ``pushing → completed`` contract.
                await repo.transition(
                    persisted, to=WorkspaceStatus.completed, reason_code="PR_OPENED"
                )
                await session.commit()

        if monitor is not None:
            _log.info(
                "executor.handoff_to_pr_monitor",
                workspace_id=workspace_id,
                pr_url=pr.url,
            )
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

    # ── Internals ──────────────────────────────────────────────────────────

    def _defaults_for(self, agent: AgentRuntime) -> AgentDefaults | None:
        defaults = defaults_with_model_overrides(
            self._config.default_models,
            base=self._config.agent_defaults,
        )
        return defaults.get(agent)

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


def _call_pr_monitor_factory(
    factory: Callable[..., _MonitorRunnerProto],
    *,
    adapter: AgentAdapter,
    profile: WorkspaceProfile,
) -> _MonitorRunnerProto:
    """Call a monitor factory with the resolved profile when it accepts it.

    ``run_awf.py`` and some existing tests predate profile-aware service
    execution and expose an adapter-only factory. The service worker uses the
    two-argument form so feature PR monitors inherit the workspace profile's
    initial review grace period.
    """
    try:
        return factory(adapter, profile)
    except TypeError as exc:
        try:
            return factory(adapter)
        except TypeError:
            raise exc from None


def _build_pr_body(ws: Workspace) -> str:
    """Standard PR description generated from the workspace's task metadata."""
    external_id = f"\n**External task ID**: {ws.task_external_id}" if ws.task_external_id else ""
    return (
        f"Automatically opened by AWF workspace `{ws.id}` "
        f"(agent: `{ws.agent}`).\n"
        f"{external_id}\n\n"
        f"### Task\n{ws.task_prompt}\n\n"
        f"---\nValidation: "
        f"{_validation_command_count(ws)} profile command(s) passed inside the workspace container.\n"
    )


def _profile_for_workspace(ws: Workspace, *, worktree_path: Path) -> WorkspaceProfile:
    if ws.resolved_profile:
        return WorkspaceProfile.model_validate(ws.resolved_profile)
    return resolve_workspace_profile(
        worktree_path=worktree_path,
        inline_profile=ws.requested_profile,
        profile_ref=ws.profile_ref or ws.env_profile or "auto",
        validation_commands=list(ws.test_commands),
    ).profile


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
        return len(profile.phases.post_agent) + len(profile.phases.validate_commands)
    return len(ws.test_commands)
