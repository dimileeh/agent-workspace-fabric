"""One-shot CLI: attach an AWF PR monitor to an already-open feature PR.

Counterpart to ``scripts/schedule_release_pr.py`` (which covers
development→main release PRs). This script handles the much more
common feature-PR case:

  - A previous AWF workspace opened a PR, then its own run failed
    before reaching the monitor phase (harness bug, flaky infra).
    The feature branch is still good; this CLI attaches the monitor
    to the existing PR without re-running the coding agent.
  - A human or another tool opened a feature PR. This CLI runs the
    same comment-addressing / CI-watching / (optional) merge
    automation against it.

Behaviour:

  1. Resolve the PR's metadata via ``gh pr view`` (head branch,
     base branch, state). Refuse closed/merged PRs — nothing for
     the monitor to do there.
  2. Write a deterministic task spec JSON to
     ``<work_dir>/feature-pr-specs/<slug>-feature-pr<N>.json``.
  3. If a ``run_awf.py`` process is already driving that spec,
     exit 0 without re-spawning (idempotency).
  4. Otherwise, ``Popen`` ``run_awf.py --config <spec> --work-dir <dir>
     --keep-state`` and detach. The monitor runs in its own process
     group so this script can exit cleanly while the monitor lives.

Exit codes:
  0 — spec written + monitor spawned (or no-op because a monitor was
      already running).
  1 — lookup failed, PR not open, bad repo URL, etc.

Usage::

    ./scripts/attach_feature_pr_monitor.py \\
        --repo git@github.com:dimileeh/aira-web.git \\
        --pr 277 \\
        --agent claude_code \\
        --companions scripts/airaweb_only.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from awf.common.commands import (  # noqa: E402
    AsyncCommandRunner,
    AsyncioSubprocessRunner,
)
from awf.common.github_client import RepoRef  # noqa: E402
from awf.runtime.feature_pr_sync import (  # noqa: E402
    FeaturePRSyncError,
    build_sync_feature_pr_task_spec,
    fetch_pr_metadata,
    is_feature_pr_monitor_running,
    task_spec_filename,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argparse factory — exposed so tests can verify flag parsing
    without the ``sys.exit`` side effect that ``parser.parse_args`` of
    ``sys.argv`` would normally have."""
    parser = argparse.ArgumentParser(
        description=(
            "Attach an AWF PR monitor to an already-open feature PR. "
            "For failed-workspace recovery or external PRs."
        )
    )
    parser.add_argument(
        "--repo",
        required=True,
        help='Repo URL or slug, e.g. "git@github.com:dimileeh/aira-web.git".',
    )
    parser.add_argument(
        "--pr",
        type=int,
        required=True,
        help="PR number to attach the monitor to.",
    )
    parser.add_argument(
        "--agent",
        default="codex",
        help="Coding CLI to use when the monitor needs to address "
        "comments or fix CI failures. Default: codex.",
    )
    parser.add_argument(
        "--no-auto-merge",
        dest="auto_merge",
        action="store_false",
        help="Opt OUT of auto-merging. Default is to auto-merge when "
        "all gates turn green — that's AWF's contract for feature→dev "
        "PRs. Pass this flag for one-off recovery runs where you want "
        "to review the final state before landing.",
    )
    parser.set_defaults(auto_merge=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/tmp/awf-realrun"),
        help="AWF work directory (shared with other AWF invocations).",
    )
    parser.add_argument(
        "--companions",
        type=Path,
        default=None,
        help="Optional JSON file with a list of companion service specs. "
        "Usually required — the monitor may invoke the coding CLI, "
        "which needs the same companion stack the PR's validation "
        "originally depended on.",
    )
    return parser.parse_args(argv)


@contextlib.contextmanager
def _per_pr_lock(work_dir: Path, pr_number: int) -> Iterator[bool]:
    """Serialize the spec-write + spawn on a per-PR lock file so
    concurrent watchdog invocations can't double-spawn.

    Yields ``True`` if the caller acquired the lock and should do the
    full attach flow; yields ``False`` if another process already holds
    it (treat as idempotent no-op, exit 0).

    The lock file persists on disk; only the ``flock`` is released on
    context exit. Leaving the file in place is fine — the next attach
    invocation will reuse it. No cleanup race because flock is
    fd-scoped, not path-scoped.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    lock_path = work_dir / f"feature-pr-monitor-{pr_number}.lock"
    fd = lock_path.open("w")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another attach invocation owns the lock — let it handle
            # the spawn, we exit cleanly.
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


async def orchestrate_attach(
    *,
    repo_url: str,
    pr_number: int,
    agent: str,
    auto_merge: bool,
    companions_path: Path | None,
    work_dir: Path,
    runner: AsyncCommandRunner,
    spawn: Callable[..., Any] = subprocess.Popen,
    process_lister: Callable[[], str] | None = None,
) -> int:
    """Main async driver — composes the steps and returns an exit code.

    Extracted from ``main`` so tests can inject a ``FakeCommandRunner``
    and a fake ``spawn`` / ``process_lister``. The real ``main`` passes
    ``AsyncioSubprocessRunner`` and ``subprocess.Popen`` / ``None``.
    """
    # Parse URL upfront so we fail fast on malformed input (no gh call).
    try:
        repo = RepoRef.from_url(repo_url)
    except ValueError as exc:
        print(f"attach-feature-pr-monitor ERROR: {exc}", file=sys.stderr)
        return 1

    # Resolve PR metadata. Refuses closed/merged PRs inside
    # fetch_pr_metadata so we don't race to provision a workspace for
    # something that can't transition.
    try:
        metadata = await fetch_pr_metadata(runner=runner, repo=repo, pr_number=pr_number)
    except FeaturePRSyncError as exc:
        print(f"attach-feature-pr-monitor ERROR: {exc}", file=sys.stderr)
        return 1

    companions = _load_companions(companions_path) if companions_path else None

    spec = build_sync_feature_pr_task_spec(
        repo_url=repo_url,
        metadata=metadata,
        agent=agent,
        auto_merge=auto_merge,
        companions=companions,
    )
    spec_filename = task_spec_filename(repo_slug=repo.slug(), pr_number=pr_number)

    # Serialize: the ps-grep idempotency check races with a concurrent
    # invocation that's already PAST the check but hasn't called Popen
    # yet. A per-PR fcntl.flock closes that window. Watchdog polls can
    # hammer attach without double-spawning.
    with _per_pr_lock(work_dir, pr_number) as acquired:
        if not acquired:
            print(
                f"attach-feature-pr-monitor: another invocation is already "
                f"attaching {repo.slug()}#{pr_number}; exiting idempotent.",
                flush=True,
            )
            return 0

        # Re-check ps INSIDE the lock — the losing invocation from a
        # previous race may have already spawned the monitor between
        # our ps check and our lock acquisition.
        if is_feature_pr_monitor_running(
            spec_filename=spec_filename, process_lister=process_lister
        ):
            print(
                f"attach-feature-pr-monitor: monitor already active for "
                f"{repo.slug()}#{pr_number}; nothing to do.",
                flush=True,
            )
            return 0

        spec_dir = work_dir / "feature-pr-specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path = spec_dir / spec_filename
        # run_awf.py expects a LIST of task specs — wrap ours in one.
        spec_path.write_text(json.dumps([spec], indent=2))

        # Spawn run_awf.py detached. We pass --keep-state so the shared
        # SQLite DB in work_dir isn't wiped on startup.
        log_path = work_dir / f"feature-pr-monitor-{pr_number}.log"
        python_bin = _ROOT / ".venv" / "bin" / "python"
        run_awf = _ROOT / "scripts" / "run_awf.py"
        handle = spawn(
            [
                str(python_bin),
                str(run_awf),
                "--config",
                str(spec_path),
                "--work-dir",
                str(work_dir),
                "--keep-state",
            ],
            stdout=log_path.open("a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(
            f"attach-feature-pr-monitor: spawned run_awf pid={getattr(handle, 'pid', '?')} "
            f"for {repo.slug()}#{pr_number}; log: {log_path}",
            flush=True,
        )
        return 0


def _load_companions(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


async def _main(ns: argparse.Namespace) -> int:
    runner = AsyncioSubprocessRunner()
    return await orchestrate_attach(
        repo_url=ns.repo,
        pr_number=ns.pr,
        agent=ns.agent,
        auto_merge=ns.auto_merge,
        companions_path=ns.companions,
        work_dir=ns.work_dir,
        runner=runner,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(parse_args())))
