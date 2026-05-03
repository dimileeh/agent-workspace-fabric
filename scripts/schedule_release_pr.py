"""One-shot CLI: ensure a development→main release PR is open for a repo.

This is the "webhook endpoint" equivalent for MVP. Invoke it whenever
you want to check a repo for divergence and open/reuse a release PR.

Callers:

* ``scripts/release_pr_watcher.py`` — invokes this on a 60-second poll
  cycle (the substitute for real GitHub webhooks in the MVP).
* A future GitHub webhook receiver could shell out to this script
  once AWF has a public HTTP surface.
* Humans can run it manually:
  ``./scripts/schedule_release_pr.py --repo git@github.com:dimileeh/aira-agent.git``

Behaviour:

1. Check the source branch for commits ahead of target. If zero, exit 0.
2. Check for an existing open release PR. If one exists, exit 0
   (AWF's release-PR monitor — if running — keeps it current).
3. Otherwise, open a fresh release PR via gh CLI.

**Does NOT** spin up an AWF workspace to monitor the PR. That's a
separate concern — the watcher (or a cron) invokes ``run_awf.py`` with
a ``sync_release_pr`` task spec when it decides a monitor should be
attached. See ``docs/PLAN_RELEASE_PR_SYNC.md`` for the full flow.

Exit codes:
  0 — success (PR opened, reused, or no divergence).
  1 — sync error (gh CLI failed, bad repo, network, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from awf.common.commands import AsyncioSubprocessRunner  # noqa: E402
from awf.common.github_client import RepoRef  # noqa: E402
from awf.runtime.release_pr_sync import (  # noqa: E402
    ReleasePrSyncError,
    ensure_release_pr_open,
)


async def _main(
    *,
    repo_url: str,
    source_branch: str,
    target_branch: str,
    dry_run: bool,
    attach_monitor: bool,
    work_dir: Path,
    agent: str,
    companions_config: Path | None,
) -> int:
    runner = AsyncioSubprocessRunner()
    repo = RepoRef.from_url(repo_url)
    try:
        result = await ensure_release_pr_open(
            runner=runner,
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            dry_run=dry_run,
        )
    except ReleasePrSyncError as exc:
        print(f"release-pr-sync ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"[{repo.slug()}] ahead_by={result.ahead_by} "
        f"pr_number={result.pr_number} created={result.created}"
    )
    print(f"  {result.reason}")
    if result.pr_url:
        print(f"  URL: {result.pr_url}")

    # If asked, and a PR exists, spawn a monitoring workspace via run_awf.py.
    # Idempotency guard: only launch if no sync_release_pr workspace is
    # already active for this PR.
    if attach_monitor and result.pr_number is not None:
        if _monitor_already_running(
            work_dir=work_dir, repo_slug=repo.slug(), pr_number=result.pr_number
        ):
            print(f"  monitor already active for {repo.slug()}#{result.pr_number}; skipping launch")
            return 0
        print("  launching release-pr monitor workspace ...")
        companions = _load_companions(companions_config) if companions_config else []
        task_spec = [
            {
                "repo_url": repo_url,
                "branch_base": target_branch,
                "task_title": f"release-monitor: {repo.slug()}#{result.pr_number}",
                "task_prompt": (
                    "AWF release-PR monitor. No coding agent runs on entry; "
                    "the monitor attaches to an already-open "
                    f"{source_branch} → {target_branch} PR and drives it "
                    "through reviewer comments + CI + notify-human. "
                    "NEVER auto-merges."
                ),
                "agent": agent,
                "test_commands": [],
                "requires_database": False,
                "task_kind": "sync_release_pr",
                "source_branch": source_branch,
                "pr_number": result.pr_number,
                "companions": companions,
            }
        ]
        # Write the task spec to a deterministic path so the watcher can
        # re-invoke without re-reading the config file; this also makes
        # it easy to inspect what the scheduler handed off.
        spec_dir = work_dir / "release-pr-specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / f"{repo.slug().replace('/', '__')}-pr{result.pr_number}.json"
        spec_path.write_text(json.dumps(task_spec, indent=2))
        print(f"  spec: {spec_path}")

        # Fire-and-forget — run_awf.py is a long-running monitor; don't
        # block this scheduler tick on it.
        log_path = work_dir / f"release-monitor-{result.pr_number}.log"
        # Spawn run_awf.py using whichever Python is currently running
        # this scheduler. Hardcoding ``_ROOT / ".venv" / "bin" / "python"``
        # only works when the caller's venv lives at exactly that path;
        # ``uv``, system python, and operator-local virtualenvs would
        # all break that assumption. Review feedback on PR #2
        # (CodeRabbit): "use the current interpreter".
        run_awf = _ROOT / "scripts" / "run_awf.py"
        with log_path.open("a") as log_file:
            subprocess.Popen(  # noqa: S603 - deliberately spawning a long-running child
                [
                    sys.executable,
                    str(run_awf),
                    "--config",
                    str(spec_path),
                    "--work-dir",
                    str(work_dir),
                    "--keep-state",
                ],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach so scheduler can exit cleanly
            )
        print(f"  monitor launched; log: {log_path}")
    return 0


# Cooldown: after a workspace for this PR terminates (completed or
# failed), don't respawn for ``_MONITOR_COOLDOWN_SECONDS``. Prevents
# retry storms when a deterministic failure (docker network pool
# exhausted, stale secret, etc.) would otherwise cause the watcher to
# burn a new workspace every tick.
#
# 2026-04-24 incident: ~180 failed sync_release_pr workspaces in 6h
# because the compose-up fail-fast path created terminal rows
# instantly, and the "non-terminal" idempotency check bypassed them.
# The cooldown is 5 min so a transient issue gets one retry per
# 5 min — enough to recover without spam.
_MONITOR_COOLDOWN_SECONDS = 300


def _monitor_already_running(*, work_dir: Path, repo_slug: str, pr_number: int) -> bool:
    """True iff we should skip spawning a new monitor for this PR.

    Skips when EITHER:
      a) a non-terminal workspace row already exists (classic "already
         running" case — another process is handling it), OR
      b) the most-recent terminal workspace row for this PR is younger
         than ``_MONITOR_COOLDOWN_SECONDS`` (cooldown after failure).

    (b) is the 2026-04-24 fix. Without it, a deterministic failure
    path (docker network pool full, auth expired, etc.) would let the
    watcher spawn a new workspace every tick (~2 min), producing 30+
    failed rows per hour and never recovering because each attempt
    fails the same way.

    Originally a process-based ``pgrep run_awf.py`` check. That was
    fragile: a run_awf.py that crashes fast leaves a ``provisioning``
    row but NO process. DB-based check catches both cases.
    """
    # SQLite DB lives at ``<work_dir>/awf.db``. The scheduler may run
    # before any workspace has been provisioned (no DB yet) — treat
    # that as "no monitor running" and let the launch proceed.
    db_path = work_dir / "awf.db"
    if not db_path.exists():
        return False
    # Use a plain sync sqlite3 connection — the scheduler isn't inside
    # the async driver's event loop and doesn't need to pull in the
    # whole SQLAlchemy stack for a single SELECT.
    import sqlite3

    repo_url_variants = _repo_url_variants(repo_slug)
    placeholders = ",".join("?" for _ in repo_url_variants)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            # (a) Non-terminal row → classic "already running".
            cur = conn.execute(
                f"""SELECT 1 FROM workspaces
                    WHERE task_kind = 'sync_release_pr'
                      AND pr_number = ?
                      AND repo_url IN ({placeholders})
                      AND status NOT IN ('completed', 'failed')
                    LIMIT 1""",
                (pr_number, *repo_url_variants),
            )
            if cur.fetchone() is not None:
                return True
            # (b) Terminal row within cooldown window → skip.
            # Compare ``updated_at`` against ``now() - cooldown`` using
            # SQLite's own time math so we don't depend on the
            # timezone the row was written in.
            cur = conn.execute(
                f"""SELECT 1 FROM workspaces
                    WHERE task_kind = 'sync_release_pr'
                      AND pr_number = ?
                      AND repo_url IN ({placeholders})
                      AND status IN ('completed', 'failed')
                      AND datetime(updated_at) >= datetime('now', ?)
                    ORDER BY updated_at DESC
                    LIMIT 1""",
                (
                    pr_number,
                    *repo_url_variants,
                    f"-{_MONITOR_COOLDOWN_SECONDS} seconds",
                ),
            )
            row = cur.fetchone()
    except sqlite3.DatabaseError:
        # Malformed / locked DB → don't let that stop the scheduler.
        # Worst case we spawn a duplicate, which is what we had before.
        return False
    return row is not None


def _repo_url_variants(repo_slug: str) -> tuple[str, ...]:
    """Return the repo-URL forms the AWF DB might have stored.

    The driver accepts SSH (``git@github.com:owner/name.git``) and
    HTTPS (``https://github.com/owner/name``) forms. We check both so
    the idempotency query doesn't miss a workspace that was recorded
    with the other flavor."""
    return (
        f"git@github.com:{repo_slug}.git",
        f"git@github.com:{repo_slug}",
        f"https://github.com/{repo_slug}.git",
        f"https://github.com/{repo_slug}",
    )


def _load_companions(companions_path: Path) -> list[dict]:
    """Companion specs are repo-dependent (aira-agent + aira-web typically
    need a backend + web + postgres). Keep them in a separate JSON so the
    scheduler's CLI stays repo-agnostic."""
    return json.loads(companions_path.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        required=True,
        help='Repo URL or slug, e.g. "git@github.com:dimileeh/aira-agent.git" '
        'or "dimileeh/aira-agent".',
    )
    parser.add_argument(
        "--source", default="development", help="Source branch (default: development)"
    )
    parser.add_argument("--target", default="main", help="Target branch (default: main)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without opening a PR.",
    )
    parser.add_argument(
        "--attach-monitor",
        action="store_true",
        help="After confirming a PR exists, launch a sync_release_pr AWF "
        "workspace that runs the release-PR monitor against it. Required "
        "to actually address comments + watch CI; without this the script "
        "only opens the PR and exits.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/tmp/awf-realrun"),
        help="AWF work directory (required when --attach-monitor is set).",
    )
    parser.add_argument(
        "--agent",
        default="codex",
        help="Which coding CLI to use for comment / CI fixes. Default codex.",
    )
    parser.add_argument(
        "--companions",
        type=Path,
        default=None,
        help="Optional JSON file with a list of companion service specs. "
        "Release-PR monitors typically don't need companions (no feature "
        "branch to test), but you can supply one if the repo's own test "
        "infrastructure (e.g. aira-web's BFF e2e) requires a backend/web "
        "stack during comment-fix CLI invocations.",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            _main(
                repo_url=args.repo,
                source_branch=args.source,
                target_branch=args.target,
                dry_run=args.dry_run,
                attach_monitor=args.attach_monitor,
                work_dir=args.work_dir,
                agent=args.agent,
                companions_config=args.companions,
            )
        )
    )
