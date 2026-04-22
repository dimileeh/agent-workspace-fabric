"""Long-running watcher: poll configured repos and ensure release PRs are open.

This is the MVP substitute for GitHub webhooks. A real webhook
integration (Phase 3, once AWF has a public HTTP surface) swaps the
polling loop for an event-driven HTTP endpoint without changing the
rest of the architecture — both call ``ensure_release_pr_open`` from
``awf.runtime.release_pr_sync``, so the observable behaviour is
identical within the poll interval.

Usage::

    ./scripts/release_pr_watcher.py \\
        --repo git@github.com:dimileeh/aira-agent.git \\
        --repo git@github.com:dimileeh/aira-web.git \\
        --interval 60

Runs until killed. Each tick:
  1. For every ``--repo``, call ``ensure_release_pr_open``.
  2. Log a one-liner per repo with the outcome.
  3. Sleep ``--interval`` seconds.

Errors on a single repo don't crash the watcher — the next tick retries.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from awf.common.commands import AsyncioSubprocessRunner  # noqa: E402
from awf.common.github_client import RepoRef  # noqa: E402
from awf.common.logging import get_logger  # noqa: E402
from awf.runtime.release_pr_sync import (  # noqa: E402
    ReleasePrSyncError,
    ensure_release_pr_open,
)

_log = get_logger(__name__)


async def _tick_one(
    *, runner: AsyncioSubprocessRunner, repo_url: str, source: str, target: str
) -> None:
    repo = RepoRef.from_url(repo_url)
    try:
        result = await ensure_release_pr_open(
            runner=runner,
            repo=repo,
            source_branch=source,
            target_branch=target,
        )
    except ReleasePrSyncError as exc:
        _log.warning(
            "watcher.sync_error",
            repo=repo.slug(),
            operation=exc.operation,
            stderr=exc.stderr[:400],
        )
        return
    _log.info(
        "watcher.tick",
        repo=repo.slug(),
        ahead_by=result.ahead_by,
        pr_number=result.pr_number,
        created=result.created,
        reason=result.reason,
    )


async def _run(
    *, repos: list[str], source: str, target: str, interval: float
) -> int:
    runner = AsyncioSubprocessRunner()
    stop = asyncio.Event()

    def _handle_signal(signum: int, _frame: object) -> None:  # type: ignore[no-untyped-def]
        _log.info("watcher.shutdown_signal", signum=signum)
        stop.set()

    # SIGTERM from orchestrators, SIGINT from Ctrl+C.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _log.info(
        "watcher.start",
        repos=[RepoRef.from_url(r).slug() for r in repos],
        source=source,
        target=target,
        interval_s=interval,
    )

    while not stop.is_set():
        # Tick all repos concurrently within a single iteration — faster
        # per-tick, and each repo's gh calls are independent.
        await asyncio.gather(
            *(
                _tick_one(runner=runner, repo_url=r, source=source, target=target)
                for r in repos
            )
        )
        # Interruptible sleep.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    _log.info("watcher.stopped")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repeatable. Each is a GitHub repo URL or slug.",
    )
    parser.add_argument("--source", default="development")
    parser.add_argument("--target", default="main")
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between ticks. Default 60.",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            _run(
                repos=args.repo,
                source=args.source,
                target=args.target,
                interval=args.interval,
            )
        )
    )
