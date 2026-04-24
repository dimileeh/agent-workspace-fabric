"""I/O orchestrator for the PR monitor.

Wraps the pure decision core (``pr_monitor.decide``) with the real side
effects: GitHub calls, docker-compose exec into the coding CLI,
filesystem git operations on the worktree, and workspace DB writes.

The loop:

1.  Load workspace row → reconstruct ``MonitorState``.
2.  Compute ``base_behind_count`` via ``git rev-list --count HEAD..origin/<base>``.
3.  Fetch ``PRStatus`` via ``GitHubClient.fetch_pr_status``.
4.  ``decide(status, state, config)`` → ``MonitorAction``.
5.  Execute the action. For ``AddressComments``, run the nested
    ``fix_cycle`` — keep committing locally while new comments keep
    arriving, and only push once a short settle window passes with no
    new activity. After the push, resolve the threads we addressed.
6.  Persist updated state.
7.  ``Merge`` / ``NotifyHuman`` / ``Abort`` / ``ShortCircuitCompleted``
    are terminal — the runner transitions the workspace and returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    fix_ci_prompt,
    ready_to_merge_comment,
    sync_base_conflict_prompt,
)
from awf.runtime.pr_monitor import (
    Abort,
    AbortReason,
    AddressComments,
    CheckFailure,
    Merge,
    MonitorAction,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    _is_bot_author,
    decide,
)

_log = get_logger(__name__)


# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.
Verdict = str  # "fix_committed" | "false_positive" | "defer"


@dataclass(frozen=True)
class MonitorRunnerConfig:
    """Operational knobs for the runner (separate from MonitorConfig so
    we can tune timing without touching the decision logic)."""

    # Max number of outer loop iterations before we stop (safety net
    # against a decision-loop bug; the outer loop is uncapped for
    # ``WaitForCI`` so idle polls don't count). A legitimate monitor
    # session should always exit via a terminal action well before this.
    max_outer_iterations: int = 10_000
    # Max fix_cycle re-polls inside a single AddressComments action.
    max_fix_cycle_passes: int = 5


@dataclass
class _RunnerDeps:
    """All side-effect collaborators in one bag — easy to fake in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    runner: AsyncCommandRunner
    adapter: AgentAdapter
    gh: GitHubClient
    sleep: Callable[[float], Awaitable[None]]


class PullRequestMonitorRunner:
    """Drives the ``monitoring_pr`` stage for a single workspace."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runner: AsyncCommandRunner,
        adapter: AgentAdapter,
        gh: GitHubClient,
        monitor_config: MonitorConfig | None = None,
        runner_config: MonitorRunnerConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        worktrees_root: Path,
        artifacts_root: Path | None = None,
    ) -> None:
        self._deps = _RunnerDeps(
            session_factory=session_factory,
            runner=runner,
            adapter=adapter,
            gh=gh,
            sleep=sleep,
        )
        self._config = monitor_config or MonitorConfig()
        self._runner_config = runner_config or MonitorRunnerConfig()
        self._worktrees_root = worktrees_root
        # Orchestrator-facing JSON drops — one ``<ws_id>.defer-signal.json``
        # per terminal transition. Default layout matches ``run_awf.py``'s
        # ``<work_dir>/artifacts`` directory; since ``worktrees_root`` there
        # is ``<work_dir>/git/worktrees``, go up two levels.
        self._artifacts_root = artifacts_root or (worktrees_root.parents[1] / "artifacts")

    # ── Entry point ────────────────────────────────────────────────────────

    async def run(self, *, workspace_id: str, compose_project: str, compose_file: Path) -> None:
        """Drive the monitor phase until a terminal ``MonitorAction`` fires."""

        for _ in range(self._runner_config.max_outer_iterations):
            ws = await self._load_workspace(workspace_id)
            if ws.status != WorkspaceStatus.monitoring_pr.value:
                # Someone else terminated us (cancel, crash recovery, etc.).
                return

            state = self._load_state(ws)
            repo = RepoRef.from_url(ws.repo_url)
            pr_number = ws.pr_number
            if pr_number is None:
                # A workspace in ``monitoring_pr`` without a PR number is
                # an upstream invariant violation (the executor/sync
                # handlers set pr_number before transitioning here).
                # Fail cleanly instead of crashing the background runner
                # with AssertionError — review feedback on PR #2 (gemini).
                await self._terminate_failed(
                    workspace_id,
                    message=(
                        "monitor: workspace reached monitoring_pr without a "
                        "pr_number — upstream provisioning must populate it"
                    ),
                )
                return

            # Refresh the worktree's remote-tracking ref BEFORE counting
            # how far behind we are. Without this, origin/<base> is frozen
            # at the time of the initial ``git worktree add`` and the
            # count silently returns 0 even after the base branch has
            # advanced on GitHub — the exact bug that let PR #335 / #336
            # exit as "ready to merge" when they were BEHIND.
            await self._fetch_base(
                worktree_path=self._worktrees_root / workspace_id,
                base_branch=ws.branch_base,
            )
            base_behind = await self._count_base_behind(
                worktree_path=self._worktrees_root / workspace_id,
                base_branch=ws.branch_base,
            )
            try:
                status = await self._deps.gh.fetch_pr_status(
                    repo=repo, pr_number=pr_number, base_behind_count=base_behind
                )
                # If we got FAILURE we want the per-check logs for the prompt.
                if status.check_state.value == "FAILURE":
                    failures = await self._deps.gh.fetch_failing_check_logs(
                        repo=repo,
                        pr_number=pr_number,
                        head_sha=status.head_sha,
                    )
                    status = _with_ci_failures(status, failures)
            except GitHubClientError as exc:
                await self._terminate_failed(
                    workspace_id,
                    message=f"monitor: github error: {exc}"[:2000],
                )
                return

            # Determine the remote push target for this workspace.
            # ``remote_push_branch`` is the canonical destination.
            #
            # Pre-migration fallback — task-kind-conditional:
            #   * ``feature_branch_pr``: ``branch_name`` (e.g. ``awf/<id>``)
            #     equals the remote branch. Safe to fall back.
            #   * sync kinds: ``branch_name`` is the LOCAL synthetic ref
            #     (``release-sync/<id>`` / ``feature-sync/<id>``) — NOT
            #     the remote branch the PR expects. Falling back would
            #     push to a new remote branch instead of updating the
            #     PR's head. Refuse and fail fast instead; the row
            #     predates this migration and must be re-attached
            #     fresh (which will populate remote_push_branch).
            remote_branch = ws.remote_push_branch
            if remote_branch is None and ws.task_kind == "feature_branch_pr":
                remote_branch = ws.branch_name
            if not remote_branch:
                # No safe push target — either missing branch entirely
                # (upstream invariant violation) or a pre-migration
                # sync row where ``branch_name`` is unsafe to reuse.
                await self._terminate_failed(
                    workspace_id,
                    message=(
                        "monitor: workspace has no remote_push_branch "
                        f"(task_kind={ws.task_kind}, branch_name="
                        f"{ws.branch_name!r}). For sync workspaces "
                        "predating the remote_push_branch migration, "
                        "re-attach the monitor via "
                        "attach_feature_pr_monitor.py so a fresh row "
                        "is provisioned with the column populated."
                    ),
                )
                return

            action = decide(status, state, self._config)
            terminal = await self._execute(
                action=action,
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=ws.branch_base,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            await self._persist_state(workspace_id, state)
            if terminal:
                return

        # Safety net — max_outer_iterations hit without a terminal action.
        await self._terminate_failed(
            workspace_id,
            message=(
                "monitor: hit max_outer_iterations without a terminal action "
                "(likely a decision loop bug)"
            ),
        )

    # ── Action dispatch ────────────────────────────────────────────────────

    async def _execute(
        self,
        *,
        action: MonitorAction,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        status: PRStatus,
        state: MonitorState,
        base_branch: str,
        remote_branch: str,
        compose_project: str,
        compose_file: Path,
    ) -> bool:
        """Execute one action. Returns True iff the monitor has reached a
        terminal state (merged / notified / aborted / short-circuited)."""

        # One structured log line per iteration, BEFORE any side effect —
        # operators grepping logs need to see which arm the decision
        # core chose without having to correlate gh / git calls
        # downstream. Regression guard for PR 342: the monitor ran 200+
        # iterations silently because only handoff_to_pr_monitor and
        # compose_teardown_ok fired.
        _log.info(
            "monitor.action",
            workspace_id=workspace_id,
            pr_number=pr_number,
            iter=state.iter_count,
            action=type(action).__name__,
            head_sha=status.head_sha[:10],
            base_behind=status.base_behind_count,
            merge_state=(status.merge_state_status.value if status.merge_state_status else None),
            unresolved_threads=len(status.unresolved_inline_threads),
            unresolved_reviews=len(status.unresolved_review_comments),
        )

        if isinstance(action, ShortCircuitCompleted):
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="ShortCircuitCompleted",
                merged=True,
                status=status,
                state=state,
            )
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=None,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        if isinstance(action, Abort):
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="Abort",
                merged=False,
                status=status,
                state=state,
            )
            await self._terminate_failed(
                workspace_id,
                message=f"monitor: abort ({action.reason.value})",
                reason_code=action.reason,
            )
            return True

        if isinstance(action, WaitForCI):
            await self._deps.sleep(self._config.poll_interval_seconds)
            return False

        if isinstance(action, SyncBase):
            await self._run_sync_base(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            state.iter_count += 1
            return False

        if isinstance(action, ReportCiFailure):
            await self._run_ci_fix(
                repo=repo,
                pr_number=pr_number,
                failures=action.failures,
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                remote_branch=remote_branch,
            )
            state.iter_count += 1
            return False

        if isinstance(action, AddressComments):
            await self._run_fix_cycle(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                initial_threads=action.threads,
                initial_reviews=action.review_comments,
                state=state,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            state.iter_count += 1
            return False

        if isinstance(action, Merge):
            try:
                merge_sha = await self._deps.gh.merge_pr(repo=repo, pr_number=pr_number)
            except GitHubClientError as exc:
                # Branch protection often blocks merges; fall back to the
                # release-PR flow rather than failing.
                _log.warning(
                    "monitor.merge_blocked_falling_back_to_notify",
                    workspace_id=workspace_id,
                    stderr=exc.stderr,
                )
                await self._deps.gh.post_comment(
                    repo=repo,
                    pr_number=pr_number,
                    body=ready_to_merge_comment(pr_number=pr_number, head_sha=status.head_sha),
                )
                self._write_defer_signal(
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    terminal_action="Merge",
                    merged=False,
                    status=status,
                    state=state,
                )
                await self._terminate_completed(
                    workspace_id,
                    pr_merge_sha=None,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
                return True
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="Merge",
                merged=True,
                status=status,
                state=state,
            )
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=merge_sha,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        if isinstance(action, NotifyHuman):
            await self._deps.gh.post_comment(
                repo=repo,
                pr_number=pr_number,
                body=ready_to_merge_comment(pr_number=pr_number, head_sha=status.head_sha),
            )
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="NotifyHuman",
                merged=False,
                status=status,
                state=state,
            )
            await self._terminate_completed(
                workspace_id,
                pr_merge_sha=None,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            return True

        # If we got here the MonitorAction union gained a variant without
        # a dispatch arm — fail loudly so tests catch it.
        raise RuntimeError(f"unhandled monitor action: {action!r}")  # pragma: no cover

    # ── AddressComments / fix_cycle ────────────────────────────────────────

    async def _run_fix_cycle(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        initial_threads: tuple[ReviewThread, ...],
        initial_reviews: tuple[ReviewComment, ...],
        state: MonitorState,
        remote_branch: str,
        compose_project: str,
        compose_file: Path,
    ) -> None:
        """Implements the commit-then-push-on-settle behaviour from the plan.

        Invokes the coding CLI once per thread/review comment (locally
        committing fixes), then polls for new comments arriving during
        the fix pass. If any new ones arrive within ``settle_interval``,
        they're addressed in the next pass. When the comment burst is
        quiet, push everything and resolve the threads we addressed.
        """
        threads_to_resolve: list[str] = []
        threads = list(initial_threads)
        reviews = list(initial_reviews)

        for _pass_num in range(self._runner_config.max_fix_cycle_passes):
            # 1) Address each item in the current batch.
            for t in threads:
                verdict = await self._address_thread(
                    repo=repo,
                    pr_number=pr_number,
                    thread=t,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
                state.mark_addressed(t.thread_id, verdict)
                if verdict != "defer":
                    threads_to_resolve.append(t.thread_id)
            for c in reviews:
                verdict = await self._address_review_comment(
                    repo=repo,
                    pr_number=pr_number,
                    comment=c,
                    compose_project=compose_project,
                    compose_file=compose_file,
                )
                state.mark_addressed(c.comment_id, verdict)

            # 2) Settle window — small sleep, then re-poll for new activity.
            await self._deps.sleep(self._config.settle_interval_seconds)
            status = await self._deps.gh.fetch_pr_status(
                repo=repo, pr_number=pr_number, base_behind_count=0
            )
            new_threads = [
                t
                for t in status.unresolved_inline_threads
                if t.thread_id not in state.threads_addressed_ids
            ]
            new_reviews = [
                c
                for c in status.unresolved_review_comments
                if c.comment_id not in state.threads_addressed_ids
            ]
            if not new_threads and not new_reviews:
                break  # burst settled
            threads = new_threads
            reviews = new_reviews
        # (If we hit max_fix_cycle_passes we still fall through to push —
        # whatever we did commit is worth shipping; next outer loop
        # iteration will re-poll and see what's left.)

        # 3) Push everything we committed.
        worktree_path = self._worktrees_root / workspace_id
        pushed = await self._git_push(worktree_path=worktree_path, remote_branch=remote_branch)
        if not pushed:
            # No local commits — CLI returned "false_positive" for
            # everything or "defer" for everything. We still want to
            # resolve the non-defer threads on GitHub.
            pass

        # 4) Resolve threads on GitHub. Only inline threads have IDs we can
        # resolve via the GraphQL mutation; review-level comments are
        # marked addressed in state and the reviewer's re-read usually
        # clears them.
        for tid in threads_to_resolve:
            try:
                await self._deps.gh.resolve_thread(thread_id=tid)
            except GitHubClientError as exc:
                _log.warning(
                    "monitor.resolve_thread_failed",
                    thread_id=tid,
                    stderr=exc.stderr,
                )
                # Do NOT drop out of the monitor — next outer poll will
                # see the thread still unresolved and retry.

        # 5) Update last_push_sha.
        if pushed:
            head_sha = await self._rev_parse_head(worktree_path)
            state.last_push_sha = head_sha

    async def _address_thread(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        thread: ReviewThread,
        compose_project: str,
        compose_file: Path,
    ) -> Verdict:
        prompt = address_thread_prompt(pr_number=pr_number, repo_slug=repo.slug(), thread=thread)
        return await self._invoke_cli_for_verdict(
            prompt=prompt,
            compose_project=compose_project,
            compose_file=compose_file,
        )

    async def _address_review_comment(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        comment: ReviewComment,
        compose_project: str,
        compose_file: Path,
    ) -> Verdict:
        prompt = address_review_comment_prompt(
            pr_number=pr_number, repo_slug=repo.slug(), comment=comment
        )
        return await self._invoke_cli_for_verdict(
            prompt=prompt,
            compose_project=compose_project,
            compose_file=compose_file,
        )

    async def _invoke_cli_for_verdict(
        self, *, prompt: str, compose_project: str, compose_file: Path
    ) -> Verdict:
        try:
            result = await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
            )
        except AgentRunError as exc:
            _log.warning(
                "monitor.cli_nonzero_exit",
                returncode=exc.result.returncode,
            )
            return "defer"
        return _parse_verdict(result.stdout)

    # ── SyncBase ───────────────────────────────────────────────────────────

    async def _run_sync_base(
        self,
        *,
        workspace_id: str,
        repo: RepoRef,
        pr_number: int,
        base_branch: str,
        remote_branch: str,
        compose_project: str,
        compose_file: Path,
    ) -> None:
        """``git fetch origin <base> && git merge origin/<base>``, push.

        On merge conflict, hand off to the coding CLI with a
        sync_base_conflict_prompt. The CLI commits the resolution; we
        push and move on.
        """
        worktree_path = self._worktrees_root / workspace_id

        async def _git(*args: str) -> tuple[int, str, str]:
            r = await self._deps.runner.run(["git", "-C", str(worktree_path), *args])
            return r.returncode, r.stdout, r.stderr

        # Defense: if a previous SyncBase attempt left the repo in a
        # MERGING state (CLI failed mid-conflict-resolve, conflicts
        # uncommitted), the next ``git merge`` would refuse with
        # "You have not concluded your merge". Abort first; the command
        # exits non-zero when there's nothing to abort, which we ignore.
        await _git("merge", "--abort")
        await _git("fetch", "origin", base_branch)
        rc, _stdout, stderr = await _git("merge", "--no-edit", f"origin/{base_branch}")
        if rc != 0:
            # Conflicts — enumerate them for the prompt.
            _rc_status, status_out, _ = await _git("status", "--porcelain")
            conflicting_files = tuple(
                line[3:]
                for line in status_out.splitlines()
                if line.startswith(("UU ", "AA ", "DD ", "AU ", "UA ", "DU ", "UD "))
            )
            prompt = sync_base_conflict_prompt(
                pr_number=pr_number,
                repo_slug=repo.slug(),
                base_branch=base_branch,
                conflicting_files=conflicting_files,
            )
            try:
                await self._deps.adapter.run(
                    compose_project=compose_project,
                    compose_file=compose_file,
                    prompt=prompt,
                )
            except AgentRunError as exc:
                _log.warning(
                    "monitor.sync_base_cli_failed",
                    workspace_id=workspace_id,
                    stderr=exc.result.stderr[:400],
                )

        # Whether or not we hit conflicts, push what we have.
        await self._git_push(worktree_path=worktree_path, remote_branch=remote_branch)

    # ── CI failure ─────────────────────────────────────────────────────────

    async def _run_ci_fix(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        failures: tuple[CheckFailure, ...],
        compose_project: str,
        compose_file: Path,
        workspace_id: str,
        remote_branch: str,
    ) -> None:
        prompt = fix_ci_prompt(pr_number=pr_number, repo_slug=repo.slug(), failures=failures)
        try:
            await self._deps.adapter.run(
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
            )
        except AgentRunError as exc:
            _log.warning(
                "monitor.ci_fix_cli_failed",
                workspace_id=workspace_id,
                stderr=exc.result.stderr[:400],
            )
        await self._git_push(
            worktree_path=self._worktrees_root / workspace_id,
            remote_branch=remote_branch,
        )

    # ── Git plumbing ───────────────────────────────────────────────────────

    async def _fetch_base(self, *, worktree_path: Path, base_branch: str) -> None:
        """``git fetch origin <base>`` — refreshes the worktree's
        remote-tracking ref so the subsequent rev-list is accurate.

        Non-fatal on failure (offline, transient network, etc.). The
        decide() gate will fall back to GitHub's mergeStateStatus if
        the local count is wrong."""
        await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "fetch",
                "origin",
                base_branch,
            ]
        )

    async def _count_base_behind(self, *, worktree_path: Path, base_branch: str) -> int:
        r = await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-list",
                "--count",
                f"HEAD..origin/{base_branch}",
            ]
        )
        if not r.ok:
            return 0
        try:
            return int(r.stdout.strip() or "0")
        except ValueError:
            return 0

    async def _rev_parse_head(self, worktree_path: Path) -> str:
        r = await self._deps.runner.run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
        return r.stdout.strip() if r.ok else ""

    async def _git_push(self, *, worktree_path: Path, remote_branch: str) -> bool:
        """Push current HEAD to ``origin/<remote_branch>`` with an
        explicit refspec.

        Returns True iff anything new was pushed.

        **Why explicit refspec, not ``git push origin HEAD``**: On
        2026-04-23 the monitor pushed four feature-branch commits to
        ``aira-web`` ``development`` because ``git push origin HEAD``
        resolves against ``push.default`` + ``branch.<current>.merge``.
        Both had been polluted by prior sync workspaces on the shared
        bare mirror (``push.default=upstream`` globally, merge config
        auto-set to ``refs/heads/development`` when worktrees branched
        from ``origin/development``). Using ``HEAD:refs/heads/<remote>``
        bypasses that entirely — the caller names the destination, git
        ignores local config. No amount of polluted config can redirect
        a push that spells its destination out.

        **Recovery on rejection**: if the push is refused because the
        remote branch has advanced past local (divergence from a prior
        monitor run whose push succeeded but whose local worktree is
        now a stale clone), this method silently resyncs local to
        remote — ``git fetch origin <remote>`` + ``git reset --hard
        origin/<remote>``. GitHub is truth for pushed state; any local
        commits that didn't make it onto the remote represent dead
        work from the failed previous push and can be safely discarded.
        The next outer-loop iteration then operates on an aligned
        worktree and its SyncBase / fix-cycle commits will fast-forward
        cleanly.

        Without this recovery, a diverged worktree caused PR #335 and
        #336 to loop until iter_cap: each failed push added another
        local merge commit, the next SyncBase piled another on top, and
        the head SHA on GitHub never moved.
        """
        refspec = f"HEAD:refs/heads/{remote_branch}"
        r = await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "push", "origin", refspec]
        )
        if r.ok:
            # git prints "Everything up-to-date" to stderr when the ref didn't move.
            return "up-to-date" not in (r.stderr or "").lower()

        # Non-zero exit. Is it a divergence rejection?
        stderr_lower = (r.stderr or "").lower()
        is_rejection = (
            "[rejected]" in stderr_lower
            or "non-fast-forward" in stderr_lower
            or "fetch first" in stderr_lower
        )
        if not is_rejection:
            # Auth, network, disk, etc. — caller retries on next poll;
            # DON'T blow away local state.
            _log.warning(
                "monitor.push_failed_non_divergence",
                stderr=(r.stderr or "")[:400],
            )
            return False

        _log.warning(
            "monitor.push_rejected_resyncing_local",
            worktree_path=str(worktree_path),
            remote_branch=remote_branch,
            stderr=(r.stderr or "")[:400],
        )
        await self._deps.runner.run(
            ["git", "-C", str(worktree_path), "fetch", "origin", remote_branch]
        )
        await self._deps.runner.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "reset",
                "--hard",
                f"origin/{remote_branch}",
            ]
        )
        return False

    # ── Defer-signal artifact ─────────────────────────────────────────────

    def _write_defer_signal(
        self,
        *,
        workspace_id: str,
        pr_number: int,
        terminal_action: str,
        merged: bool,
        status: PRStatus,
        state: MonitorState,
    ) -> None:
        """Persist a machine-readable drop of the workspace's terminal
        state for an orchestrator to consume.

        The file always exists when the runner reaches a terminal
        action — empty ``deferred_*_items`` lists when nothing was
        deferred. Downstream tooling can therefore poll for the file's
        presence as the authoritative "monitor is done" signal without
        also having to handle a missing-file case.

        Called with a best-effort contract: a failure to write MUST NOT
        stop the state-machine transition (the DB write has priority).
        """
        try:
            self._artifacts_root.mkdir(parents=True, exist_ok=True)
            bot_items, human_items = _collect_defer_items(status, state)
            payload = {
                "workspace_id": workspace_id,
                "pr_number": pr_number,
                "terminal_action": terminal_action,
                "merged": merged,
                "deferred_bot_items": bot_items,
                "deferred_human_items": human_items,
            }
            out_path = self._artifacts_root / f"{workspace_id}.defer-signal.json"
            # Atomic publish: write to a sibling temp file, then rename.
            # Pollers treat presence of out_path as the terminal signal, so
            # they must never observe a partially-written JSON payload.
            tmp_path = out_path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2))
            tmp_path.replace(out_path)
        except Exception as exc:
            _log.warning(
                "monitor.defer_signal_write_failed",
                workspace_id=workspace_id,
                error=repr(exc)[:400],
            )

    # ── DB state management ───────────────────────────────────────────────

    async def _load_workspace(self, workspace_id: str) -> Workspace:
        async with self._deps.session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            if ws is None:
                raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
            return ws

    def _load_state(self, ws: Workspace) -> MonitorState:
        started_raw = ws.monitor_started_at
        # ``MonitorState.started_at`` is monotonic; tests prefer wall-clock
        # semantics so we reconstruct by subtracting the elapsed seconds.
        # If monitor_started_at is unset (just entered monitoring_pr), use now.
        import time as _time  # local to avoid confusion with datetime above

        if started_raw is None:
            started_at = _time.monotonic()
        else:
            started_dt = started_raw
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - started_dt).total_seconds()
            started_at = _time.monotonic() - max(elapsed, 0.0)
        return MonitorState(
            iter_count=ws.monitor_iter_count,
            last_push_sha=ws.monitor_last_commit_sha,
            threads_addressed_ids=dict(ws.monitor_threads_addressed or {}),
            started_at=started_at,
        )

    async def _persist_state(self, workspace_id: str, state: MonitorState) -> None:
        async with self._deps.session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            if ws is None:
                return
            ws.monitor_iter_count = state.iter_count
            ws.monitor_threads_addressed = dict(state.threads_addressed_ids)
            if state.last_push_sha is not None:
                ws.monitor_last_commit_sha = state.last_push_sha
            if ws.monitor_started_at is None:
                ws.monitor_started_at = datetime.now(UTC)
            await s.commit()

    async def _terminate_completed(
        self,
        workspace_id: str,
        *,
        pr_merge_sha: str | None,
        compose_project: str | None = None,
        compose_file: Path | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            if pr_merge_sha:
                ws.pr_merge_sha = pr_merge_sha
            await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="MONITOR_DONE")
            await s.commit()
        # Tear down the workspace's compose stack now that its PR was
        # merged (or short-circuited because it was already merged).
        # Running stacks hold network subnets from Docker's finite
        # default pool; leaking them is what caused the 2026-04-24
        # ``all predefined address pools have been fully subnetted``
        # storm that took AWF offline for ~8 hours. User's rule: only
        # tear down on COMPLETED, never on FAILED — failed workspaces
        # stay up for operator inspection.
        #
        # Best-effort: any error here is logged but never masks the
        # completion signal. The DB transition already landed above.
        if compose_project and compose_file is not None:
            await self._teardown_compose_stack(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
            )

    async def _teardown_compose_stack(
        self,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
    ) -> None:
        """Run ``docker compose down --remove-orphans --volumes`` for a
        terminated workspace. Never raises a regular ``Exception``.

        The call is wrapped in ``except Exception`` so the failure modes
        that routinely bubble up — ``FileNotFoundError`` (no ``docker``
        on PATH, common on dev laptops without the daemon), transient
        I/O errors from the subprocess runner, compose returning junk
        stderr — don't fail a workspace that already merged its PR. The
        DB completion transition has already landed before this method
        runs.

        ``asyncio.CancelledError`` is intentionally NOT caught here
        (since Python 3.8 it inherits from ``BaseException``, so the
        ``except Exception`` clause does not match it). Cancellation
        must propagate cleanly — swallowing it would defeat the loop
        runner's shutdown path."""
        try:
            r = await self._deps.runner.run(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    compose_project,
                    "--file",
                    str(compose_file),
                    "down",
                    "--remove-orphans",
                    "--volumes",
                ]
            )
        except Exception as exc:
            # docker binary missing, transient I/O, subprocess-runner
            # hiccup — any of these would otherwise propagate and crash
            # the monitor runner. Log and swallow; the DB transition
            # already completed. Cancellation (BaseException) is not in
            # this branch by design — it flows through.
            _log.warning(
                "monitor.compose_teardown_raised",
                workspace_id=workspace_id,
                compose_project=compose_project,
                error=repr(exc)[:400],
            )
            return

        if r.ok:
            _log.info(
                "monitor.compose_teardown_ok",
                workspace_id=workspace_id,
                compose_project=compose_project,
            )
        else:
            # Compose may already be gone (operator tore it down
            # manually, or an earlier teardown in a retry loop).
            _log.warning(
                "monitor.compose_teardown_failed",
                workspace_id=workspace_id,
                compose_project=compose_project,
                returncode=r.returncode,
                stderr=(r.stderr or "")[:400],
            )

    async def _terminate_failed(
        self,
        workspace_id: str,
        *,
        message: str,
        reason_code: AbortReason | None = None,
    ) -> None:
        async with self._deps.session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message
            rc = reason_code.value if reason_code else "MONITOR_ABORT"
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code=rc)
            await s.commit()


# ── Helpers ────────────────────────────────────────────────────────────────


_VERDICT_FALSE_POSITIVE = re.compile(r"\bFALSE\s+POSITIVE\s*:", re.IGNORECASE)
_VERDICT_DEFER = re.compile(r"\bDEFER\s*:", re.IGNORECASE)


def _parse_verdict(stdout: str) -> Verdict:
    """Map the CLI's final message to a structured verdict.

    The prompt templates instruct the CLI to start its reply with one of
    ``FALSE POSITIVE:`` / ``DEFER:`` / (implicit) fix-committed. We scan
    for those markers in the captured stdout; anything else counts as a
    fix commit (the default happy path).
    """
    if not stdout:
        return "defer"
    if _VERDICT_FALSE_POSITIVE.search(stdout):
        return "false_positive"
    if _VERDICT_DEFER.search(stdout):
        return "defer"
    return "fix_committed"


def _with_ci_failures(status: PRStatus, failures: tuple[CheckFailure, ...]) -> PRStatus:
    """Immutable-replace ci_failures on a ``PRStatus`` (frozen dataclass)."""
    # Import dataclasses.replace locally to keep the top-level imports tight.
    from dataclasses import replace

    return replace(status, ci_failures=failures)


def _collect_defer_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Collect deferred threads/comments, partitioned by author kind.

    Returns ``(bot_items, human_items)``. Items whose author classifies
    as a bot per ``pr_monitor._is_bot_author`` go into the first list;
    the rest (including unknown-author items, which the merge gate
    treats as human for safety) go into the second — the artifact
    mirrors that classification so orchestrators see the same picture.
    """
    bot_items: list[dict[str, object]] = []
    human_items: list[dict[str, object]] = []
    for t in status.unresolved_inline_threads:
        if state.threads_addressed_ids.get(t.thread_id) != "defer":
            continue
        bucket = bot_items if _is_bot_author(t.author) else human_items
        bucket.append(
            {
                "kind": "thread",
                "id": t.thread_id,
                "author": t.author,
                "path": t.path,
                "line": t.line,
                "body": t.body_excerpt,
                "agent_verdict_reason": None,
            }
        )
    for c in status.unresolved_review_comments:
        if state.threads_addressed_ids.get(c.comment_id) != "defer":
            continue
        bucket = bot_items if _is_bot_author(c.author) else human_items
        bucket.append(
            {
                "kind": "review",
                "id": c.comment_id,
                "author": c.author,
                "path": None,
                "line": None,
                "body": c.body_excerpt,
                "agent_verdict_reason": None,
            }
        )
    return bot_items, human_items
