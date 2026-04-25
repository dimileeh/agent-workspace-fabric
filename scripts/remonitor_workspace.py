"""Re-enter the PR monitor on a workspace that was previously marked
``completed`` but whose PR is actually still open + mergeable.

Use case: before commit 7657ced the monitor silently fell back to
``NotifyHuman`` on a BEHIND/DIRTY state, completing the workspace while
leaving the PR open. This one-shot script resets the workspace back to
``monitoring_pr`` and invokes the (now-fixed) monitor, which handles
``SyncBase`` correctly — ``git merge origin/<base>`` + LLM conflict
resolution + re-merge.

Usage:
    ./scripts/remonitor_workspace.py \\
        --work-dir /tmp/awf-realrun --workspace-id ws_<id>

Preserves the existing ``monitor_threads_addressed`` so the monitor
doesn't re-poke CodeRabbit/Cursor threads we already resolved. Resets
``monitor_iter_count`` to 0 — the "just close this out" phase gets a
fresh budget.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Populate the adapter registry.
import awf.adapters.claude_code  # noqa: E402, F401
import awf.adapters.codex  # noqa: E402, F401
import awf.adapters.gemini  # noqa: E402, F401
from awf.adapters.base import get_adapter  # noqa: E402
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS  # noqa: E402
from awf.common.commands import AsyncCommandRunner, AsyncioSubprocessRunner  # noqa: E402
from awf.common.github_client import GitHubClient  # noqa: E402
from awf.common.ids import new_event_id  # noqa: E402
from awf.db.enums import AgentRuntime, WorkspaceStatus  # noqa: E402
from awf.db.models import WorkspaceEvent  # noqa: E402
from awf.db.repositories import WorkspaceRepository  # noqa: E402
from awf.db.session import make_session_factory  # noqa: E402
from awf.runtime.logs import LogStore  # noqa: E402
from awf.runtime.pr_monitor_runner import (  # noqa: E402
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_wall_seconds,
    _initial_review_grace_wall_started_value_from_datetime,
)
from awf.runtime.release_pr_monitor import (  # noqa: E402
    build_feature_pr_monitor,
    build_release_pr_monitor,
)


async def _main(
    work_dir: Path,
    workspace_id: str,
    *,
    auto_merge: bool = True,
    push_pending: bool = False,
) -> int:
    db_path = work_dir / "awf.db"
    if not db_path.exists():
        print(f"No AWF DB at {db_path}", file=sys.stderr)
        return 2

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = make_session_factory(engine)

    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        if ws is None:
            print(f"No workspace {workspace_id}", file=sys.stderr)
            return 2
        print(
            f"Remonitor target: {workspace_id}\n"
            f"  status now:   {ws.status}\n"
            f"  agent:        {ws.agent}\n"
            f"  branch:       {ws.branch_name}\n"
            f"  pr_url:       {ws.pr_url}\n"
            f"  pr_number:    {ws.pr_number}\n"
            f"  threads:      {len(ws.monitor_threads_addressed or {})} already addressed",
            flush=True,
        )
        if ws.pr_number is None:
            print("Workspace has no pr_number; nothing to re-monitor.", file=sys.stderr)
            return 2
        # Bypass state-machine: we're deliberately re-entering the monitor
        # phase after a premature completion. Reset iter_count AND
        # ``monitor_started_at`` so the wall-clock budget starts fresh
        # from this remonitor call — otherwise ``decide()`` keeps
        # comparing ``now()`` against the original entry timestamp and
        # an old workspace re-entered after its wall-clock cap has
        # already elapsed aborts on the first tick. KEEP
        # ``monitor_threads_addressed`` so we don't re-poke CodeRabbit
        # threads we already resolved. Review feedback on PR #2
        # (CodeRabbit): flag the wall-clock-cap reset pattern.
        #
        # We append a ``WorkspaceEvent`` by hand because the state-machine
        # assert_transition refuses ``completed → monitoring_pr``. Without
        # this, the workspace_events log has a gap between the original
        # MONITOR_DONE entry and whatever the re-attached monitor emits
        # next — operators auditing lifecycle would not see the reset.
        old_state = ws.status
        ws.status = WorkspaceStatus.monitoring_pr.value
        ws.failure_reason = None
        ws.failure_message = None
        ws.monitor_iter_count = 0
        ws.monitor_threads_addressed = _preserve_initial_review_grace_state(
            ws.monitor_threads_addressed,
            pr_number=ws.pr_number,
            monitor_started_at=ws.monitor_started_at,
        )
        ws.monitor_started_at = None
        ws.events.append(
            WorkspaceEvent(
                id=new_event_id(),
                event_type="workspace.remonitor_reset",
                old_state=old_state,
                new_state=WorkspaceStatus.monitoring_pr.value,
                reason_code="OPERATOR_REMONITOR",
            )
        )
        await s.commit()
        agent_runtime = AgentRuntime(ws.agent)
        compose_project = ws.compose_project_name or f"awf_{workspace_id}"
        remote_push_branch = ws.remote_push_branch or ws.branch_name
        if not remote_push_branch:
            print(
                "Workspace has no remote_push_branch or branch_name; nothing safe to push.",
                file=sys.stderr,
            )
            return 2

    # Re-use the container + worktree that the original run set up.
    compose_file = (
        Path(ws.compose_file_path)
        if ws.compose_file_path
        else work_dir / "compose" / "compose" / workspace_id / "compose.yml"
    )
    worktrees_root = work_dir / "git" / "worktrees"
    if not compose_file.exists():
        print(
            f"Compose file missing at {compose_file} — the workspace's "
            "containers may have been torn down. Abort.",
            file=sys.stderr,
        )
        return 2

    runner = AsyncioSubprocessRunner()
    log_store = LogStore(root=work_dir / "logs", session_factory=factory)
    if push_pending:
        await _push_pending_head(
            runner=runner,
            factory=factory,
            workspace_id=workspace_id,
            worktree_path=worktrees_root / workspace_id,
            remote_push_branch=remote_push_branch,
        )
    adapter = get_adapter(
        agent_runtime,
        runner=runner,
        defaults=DEFAULT_AGENT_DEFAULTS.get(agent_runtime),
        log_store=log_store,
    )
    gh = GitHubClient(runner)
    monitor_builder = build_feature_pr_monitor if auto_merge else build_release_pr_monitor
    monitor = monitor_builder(
        session_factory=factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        worktrees_root=worktrees_root,
        artifacts_root=work_dir / "artifacts",
        log_store=log_store,
    )

    print("[remonitor] entering monitor loop ...", flush=True)
    await monitor.run(
        workspace_id=workspace_id,
        compose_project=compose_project,
        compose_file=compose_file,
    )

    async with factory() as s:
        final = await WorkspaceRepository(s).get(workspace_id)
        assert final is not None
        print(
            f"[remonitor] done.\n"
            f"  status:         {final.status}\n"
            f"  pr_url:         {final.pr_url}\n"
            f"  pr_merge_sha:   {final.pr_merge_sha}\n"
            f"  failure_reason: {final.failure_reason}\n"
            f"  message:        {final.failure_message}",
            flush=True,
        )
    await engine.dispose()
    return 0 if final.status == WorkspaceStatus.completed.value else 1


def _preserve_initial_review_grace_state(
    monitor_threads_addressed: dict[str, str] | None,
    *,
    pr_number: int,
    monitor_started_at: datetime | None,
) -> dict[str, str]:
    threads = dict(monitor_threads_addressed or {})
    started_key = _initial_review_grace_started_key(pr_number)
    done_key = _initial_review_grace_done_key(pr_number)
    if threads.get(done_key) == "elapsed" or monitor_started_at is None:
        return threads

    started_dt = monitor_started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    if (
        started_key in threads
        and _initial_review_grace_wall_seconds(threads[started_key]) is not None
    ):
        return threads
    threads[started_key] = _initial_review_grace_wall_started_value_from_datetime(started_dt)
    return threads


async def _push_pending_head(
    *,
    runner: AsyncCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    worktree_path: Path,
    remote_push_branch: str,
) -> None:
    """Push a remonitor worktree's current HEAD before entering the loop.

    This is recovery glue for a monitor process that died mid-fix-cycle:
    the coding agent may have committed real fixes locally, but the runner
    never reached its normal post-settle push. Keep the push in this AWF
    operator tool instead of asking a human to run a raw git command.
    """
    refspec = f"HEAD:refs/heads/{remote_push_branch}"
    result = await runner.run(["git", "-C", str(worktree_path), "push", "origin", refspec])
    if not result.ok:
        print(
            "remonitor: pending-head push failed; continuing so the monitor "
            f"can retry later. stderr: {(result.stderr or '')[:400]}",
            file=sys.stderr,
        )
        return
    head = await runner.run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"])
    if not head.ok:
        return
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        if ws is None:
            return
        ws.monitor_last_commit_sha = head.stdout.strip() or None
        await s.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--no-auto-merge",
        dest="auto_merge",
        action="store_false",
        help="Use the release/manual monitor mode: notify and wait for a human merge.",
    )
    parser.set_defaults(auto_merge=True)
    parser.add_argument(
        "--push-pending",
        action="store_true",
        help="Push the workspace worktree HEAD to its PR branch before re-entering monitoring.",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            _main(
                args.work_dir,
                args.workspace_id,
                auto_merge=args.auto_merge,
                push_pending=args.push_pending,
            )
        )
    )
