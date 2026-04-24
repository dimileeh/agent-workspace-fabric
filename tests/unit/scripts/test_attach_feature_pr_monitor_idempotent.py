"""Tests for the file-lock idempotency guard in
``scripts/attach_feature_pr_monitor.py``.

The existing ``process_lister``-based idempotency (grep ``run_awf.py``
for the spec path) closes the common case: "a monitor is already alive,
don't start a second". But the watchdog (``awf.cli.watchdog``) can fire
multiple invocations per poll cycle, so there's a narrow race:

1. Invocation A runs process_lister → "no monitor alive".
2. Invocation B runs process_lister → "no monitor alive".
3. A spawns run_awf.py (monitor #1).
4. B spawns run_awf.py (monitor #2). ← double-spawn.

The fix is to serialize the spec-write + spawn with an ``fcntl.flock``
on a per-PR lock file. These tests lock in the invariant that two
concurrent invocations can't both spawn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from awf.common.commands import FakeCommandRunner
from scripts import attach_feature_pr_monitor as cli


def _gh_pr_view_ok(pr_number: int = 277) -> str:
    return json.dumps(
        {
            "number": pr_number,
            "headRefName": "fix/foo",
            "baseRefName": "development",
            "state": "OPEN",
            "isDraft": False,
            "closed": False,
            "merged": False,
            "author": {"login": "dimileeh"},
            "url": f"https://github.com/dimileeh/aira-web/pull/{pr_number}",
            "title": "fix: foo",
        }
    )


class _SpawnCapture:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))

        class _Dummy:
            pid = 12345

        return _Dummy()


class TestDoubleAttachNoDoubleSpawn:
    def test_two_sequential_invocations_spawn_only_once(self, tmp_path: Path) -> None:
        """First call → spawn. Second call (process_lister now returns a
        matching ps line for the spawned monitor) → no spawn. This is
        the baseline idempotency that must still hold after the
        file-lock addition."""
        runner_a = FakeCommandRunner()
        runner_a.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn = _SpawnCapture()

        spec_filename = "dimileeh__aira-web-feature-pr277.json"
        asyncio.run(
            cli.orchestrate_attach(
                repo_url="git@github.com:dimileeh/aira-web.git",
                pr_number=277,
                agent="claude_code",
                auto_merge=False,
                companions_path=None,
                work_dir=tmp_path,
                runner=runner_a,
                spawn=spawn,
                process_lister=lambda: "",
            )
        )
        assert len(spawn.calls) == 1

        # Second invocation: process_lister reports the running monitor.
        runner_b = FakeCommandRunner()
        runner_b.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        asyncio.run(
            cli.orchestrate_attach(
                repo_url="git@github.com:dimileeh/aira-web.git",
                pr_number=277,
                agent="claude_code",
                auto_merge=False,
                companions_path=None,
                work_dir=tmp_path,
                runner=runner_b,
                spawn=spawn,
                process_lister=lambda: f"12345 run_awf.py --config {spec_filename}\n",
            )
        )
        assert len(spawn.calls) == 1  # still 1 — no double-spawn


class TestConcurrentAttachFileLock:
    def test_file_lock_serializes_concurrent_invocations(self, tmp_path: Path) -> None:
        """Simulate two racing invocations: both pass the process_lister
        check (empty — no monitor alive yet). Only one should spawn; the
        other must exit 0 cleanly.

        The real mechanism is an ``fcntl.flock`` on
        ``<work_dir>/feature-pr-monitor-<PR>.lock``. We exercise it by
        having the first invocation hold the lock (by intercepting its
        spawn and holding the lock file open) while the second tries
        to acquire. The second must NOT spawn.
        """
        import fcntl

        lock_path = tmp_path / "feature-pr-monitor-277.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Pre-acquire the lock in the test process to simulate a racing
        # invocation that's holding it. The CLI under test should see
        # the lock is held and bail out without spawning.
        held_fd = lock_path.open("w")
        fcntl.flock(held_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            runner = FakeCommandRunner()
            runner.queue_result(returncode=0, stdout=_gh_pr_view_ok())
            spawn = _SpawnCapture()

            exit_code = asyncio.run(
                cli.orchestrate_attach(
                    repo_url="git@github.com:dimileeh/aira-web.git",
                    pr_number=277,
                    agent="claude_code",
                    auto_merge=False,
                    companions_path=None,
                    work_dir=tmp_path,
                    runner=runner,
                    spawn=spawn,
                    process_lister=lambda: "",
                )
            )

            # Exit 0 idempotent — the racing holder owns the spawn.
            assert exit_code == 0
            # No spawn fired — the lock was held.
            assert spawn.calls == []
        finally:
            fcntl.flock(held_fd.fileno(), fcntl.LOCK_UN)
            held_fd.close()

    def test_lock_released_allows_next_invocation(self, tmp_path: Path) -> None:
        """Once the first invocation completes (lock released), a
        subsequent call can acquire and spawn if the monitor isn't
        actually running yet."""
        runner_a = FakeCommandRunner()
        runner_a.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn_a = _SpawnCapture()
        asyncio.run(
            cli.orchestrate_attach(
                repo_url="git@github.com:dimileeh/aira-web.git",
                pr_number=277,
                agent="claude_code",
                auto_merge=False,
                companions_path=None,
                work_dir=tmp_path,
                runner=runner_a,
                spawn=spawn_a,
                process_lister=lambda: "",
            )
        )
        assert len(spawn_a.calls) == 1

        # Subsequent invocation with the lock released AND no monitor
        # in ps (e.g. OOM'd immediately) can still attach.
        runner_b = FakeCommandRunner()
        runner_b.queue_result(returncode=0, stdout=_gh_pr_view_ok())
        spawn_b = _SpawnCapture()
        asyncio.run(
            cli.orchestrate_attach(
                repo_url="git@github.com:dimileeh/aira-web.git",
                pr_number=277,
                agent="claude_code",
                auto_merge=False,
                companions_path=None,
                work_dir=tmp_path,
                runner=runner_b,
                spawn=spawn_b,
                process_lister=lambda: "",
            )
        )
        assert len(spawn_b.calls) == 1
