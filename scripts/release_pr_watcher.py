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


def _monitor_already_running(*, work_dir: Path, repo_slug: str, pr_number: int) -> bool:
    """Mirror of ``schedule_release_pr.py._monitor_already_running`` — we
    duplicate a handful of lines so the watcher doesn't import the CLI
    module (which has argparse top-level execution). Both check for a
    running ``run_awf.py`` process whose spec filename matches."""
    import subprocess

    spec_filename = f"{repo_slug.replace('/', '__')}-pr{pr_number}.json"
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "run_awf.py"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return any(spec_filename in line for line in out.splitlines())


async def _tick_one(
    *,
    runner: AsyncioSubprocessRunner,
    repo_url: str,
    source: str,
    target: str,
    work_dir: Path,
    attach_monitor: bool,
    agent: str,
    companions_path: Path | None,
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
    if not attach_monitor or result.pr_number is None:
        return
    if _monitor_already_running(
        work_dir=work_dir, repo_slug=repo.slug(), pr_number=result.pr_number
    ):
        _log.debug(
            "watcher.monitor_already_running",
            repo=repo.slug(),
            pr_number=result.pr_number,
        )
        return
    # Delegate to schedule_release_pr.py in attach-monitor mode so we
    # reuse its spec-writing + process-spawning logic exactly. This
    # keeps a single code path for "spawn a monitor" whether it was
    # triggered by the watcher or by a human running the CLI manually.
    _log.info(
        "watcher.spawning_monitor",
        repo=repo.slug(),
        pr_number=result.pr_number,
    )
    scheduler = Path(__file__).resolve().parent / "schedule_release_pr.py"
    args = [
        sys.executable,
        str(scheduler),
        "--repo",
        repo_url,
        "--source",
        source,
        "--target",
        target,
        "--attach-monitor",
        "--work-dir",
        str(work_dir),
        "--agent",
        agent,
    ]
    if companions_path is not None:
        args.extend(["--companions", str(companions_path)])
    # Run the scheduler via asyncio so we neither block the watcher's
    # event loop nor silently drop the scheduler's exit status.
    # ``subprocess.run`` on the sync path would pause every other
    # repo's tick while this scheduler executed; ``asyncio.create_sub
    # process_exec`` lets the loop continue servicing the other
    # coroutines scheduled by ``gather()`` in ``_run``. Review
    # feedback on PR #2 (CodeRabbit): "don't block the event loop or
    # ignore scheduler launch failures".
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        _log.warning(
            "watcher.scheduler_launch_failed",
            repo=repo.slug(),
            pr_number=result.pr_number,
            returncode=proc.returncode,
            stderr=stderr.decode("utf-8", errors="replace")[:400],
        )
    else:
        _log.info(
            "watcher.scheduler_launched",
            repo=repo.slug(),
            pr_number=result.pr_number,
            stdout_snippet=stdout.decode("utf-8", errors="replace")[:200],
        )


async def _run(
    *,
    repos: list[str],
    source: str,
    target: str,
    interval: float,
    work_dir: Path,
    attach_monitor: bool,
    agent: str,
    companions_path: Path | None,
) -> int:
    runner = AsyncioSubprocessRunner()
    stop = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        _log.info("watcher.shutdown_signal", signum=int(sig))
        stop.set()

    # SIGTERM from orchestrators, SIGINT from Ctrl+C. Use the asyncio-native
    # registration path so the handler runs on the event loop (safe to call
    # ``stop.set()``) rather than from a C-level signal handler, where the
    # scheduling hop to the loop can be delayed by whatever coroutine is
    # currently holding the thread — per CPython asyncio docs.
    #
    # ``loop.add_signal_handler`` raises ``NotImplementedError`` on
    # Windows event loops per the asyncio docs. The watcher is Linux-only
    # in production, but the fallback keeps dev-on-Windows usable: drop
    # back to ``signal.signal`` + ``call_soon_threadsafe`` so the C-level
    # handler hops to the loop safely.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            signal.signal(
                sig,
                lambda _signum, _frame, s=sig: loop.call_soon_threadsafe(_handle_signal, s),
            )

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
                _tick_one(
                    runner=runner,
                    repo_url=r,
                    source=source,
                    target=target,
                    work_dir=work_dir,
                    attach_monitor=attach_monitor,
                    agent=agent,
                    companions_path=companions_path,
                )
                for r in repos
            )
        )
        # Interruptible sleep.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
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
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/tmp/awf-realrun"),
        help="AWF work directory.",
    )
    parser.add_argument(
        "--attach-monitor",
        action="store_true",
        help="After opening / detecting a release PR, also launch a "
        "sync_release_pr AWF workspace that attaches the release-PR "
        "monitor. With this flag the watcher becomes a full "
        "open-PR-and-monitor pipeline; without it, it only opens/detects.",
    )
    parser.add_argument("--agent", default="codex")
    parser.add_argument(
        "--companions",
        type=Path,
        default=None,
        help="Path to a JSON file with companion service specs.",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            _run(
                repos=args.repo,
                source=args.source,
                target=args.target,
                interval=args.interval,
                work_dir=args.work_dir,
                attach_monitor=args.attach_monitor,
                agent=args.agent,
                companions_path=args.companions,
            )
        )
    )
