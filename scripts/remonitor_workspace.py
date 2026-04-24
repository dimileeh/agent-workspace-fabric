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
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

# Populate the adapter registry.
import awf.adapters.claude_code  # noqa: E402, F401
import awf.adapters.codex  # noqa: E402, F401
import awf.adapters.gemini  # noqa: E402, F401
from awf.adapters.base import get_adapter  # noqa: E402
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS  # noqa: E402
from awf.common.commands import AsyncioSubprocessRunner  # noqa: E402
from awf.common.github_client import GitHubClient  # noqa: E402
from awf.common.ids import new_event_id  # noqa: E402
from awf.db.enums import AgentRuntime, WorkspaceStatus  # noqa: E402
from awf.db.models import WorkspaceEvent  # noqa: E402
from awf.db.repositories import WorkspaceRepository  # noqa: E402
from awf.db.session import make_session_factory  # noqa: E402
from awf.runtime.release_pr_monitor import build_feature_pr_monitor  # noqa: E402


async def _main(work_dir: Path, workspace_id: str) -> int:
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

    # Re-use the container + worktree that the original run set up.
    compose_file = work_dir / "compose" / "compose" / workspace_id / "compose.yml"
    worktrees_root = work_dir / "git" / "worktrees"
    if not compose_file.exists():
        print(
            f"Compose file missing at {compose_file} — the workspace's "
            "containers may have been torn down. Abort.",
            file=sys.stderr,
        )
        return 2

    runner = AsyncioSubprocessRunner()
    adapter = get_adapter(
        agent_runtime,
        runner=runner,
        defaults=DEFAULT_AGENT_DEFAULTS.get(agent_runtime),
    )
    gh = GitHubClient(runner)
    monitor = build_feature_pr_monitor(
        session_factory=factory,
        runner=runner,
        adapter=adapter,
        gh=gh,
        worktrees_root=worktrees_root,
        artifacts_root=work_dir / "artifacts",
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.work_dir, args.workspace_id)))
