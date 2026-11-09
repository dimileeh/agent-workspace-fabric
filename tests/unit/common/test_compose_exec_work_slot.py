"""Work-slot invocation marker and process probe on tracked compose exec (slot-release).

Agent and validation ``awf-exec`` runs are tagged with
``AWF_WORK_SLOT_INVOCATION_ID=<invocation>`` so a restarted worker (and the
periodic reconciler) can prove whether a lease's process is still in flight before
adopting or releasing it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from awf.common import compose_exec
from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.common.compose_exec import (
    build_tracked_compose_exec,
    build_work_slot_probe_compose_exec,
    probe_work_slot_invocation,
)
from awf.common.work_slot import WORK_SLOT_INVOCATION_ENV

pytestmark = pytest.mark.unit

_COMPOSE_FILE = Path("/tmp/ws/compose.yml")


def _invocation(**kwargs: object) -> compose_exec.TrackedComposeExec:
    return build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=_COMPOSE_FILE,
        cli_args=["codex", "exec"],
        source="agent",
        label="codex",
        invocation_id="awf_test_invocation",
        **kwargs,  # type: ignore[arg-type]
    )


def test_work_slot_marker_is_passed_as_exec_env_before_the_service() -> None:
    invocation = _invocation(work_slot_invocation_id="wsl_abc123")

    args = invocation.args
    marker = f"{WORK_SLOT_INVOCATION_ENV}=wsl_abc123"
    assert marker in args
    marker_index = args.index(marker)
    assert args[marker_index - 1] == "-e"
    assert marker_index < args.index("agent")


def test_without_work_slot_the_exec_argv_is_unchanged() -> None:
    assert _invocation().args == _invocation(work_slot_invocation_id=None).args
    assert not any(WORK_SLOT_INVOCATION_ENV in arg for arg in _invocation().args)


def test_work_slot_marker_rejects_unsafe_invocation_ids() -> None:
    with pytest.raises(ValueError, match="invocation_id"):
        _invocation(work_slot_invocation_id="wsl;rm -rf /")
    with pytest.raises(ValueError, match="invocation_id"):
        build_work_slot_probe_compose_exec(
            compose_project="awf_ws_123",
            compose_file=_COMPOSE_FILE,
            invocation_id="$(id)",
        )


def test_probe_command_targets_the_agent_service_with_the_exact_marker() -> None:
    args = build_work_slot_probe_compose_exec(
        compose_project="awf_ws_123",
        compose_file=_COMPOSE_FILE,
        invocation_id="wsl_abc123",
    )

    assert args[:8] == [
        "docker",
        "compose",
        "-p",
        "awf_ws_123",
        "-f",
        str(_COMPOSE_FILE),
        "exec",
        "-T",
    ]
    assert "agent" in args
    assert args[-2:] == ["awf-work-slot-probe", "wsl_abc123"]
    script = args[-3]
    assert f"{WORK_SLOT_INVOCATION_ENV}=wsl_abc123" in script
    assert "/proc/[0-9]*/environ" in script
    assert "kill" not in script


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("awf work-slot probe: present wsl_abc123\n", 0, "present"),
        ("awf work-slot probe: absent wsl_abc123\n", 0, "absent"),
        ("garbage\n", 0, "unknown"),
        ("", 1, "unknown"),
    ],
)
async def test_probe_parses_present_absent_and_unknown(
    stdout: str, returncode: int, expected: str
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=returncode, stdout=stdout, stderr="boom" if returncode else "")

    result = await probe_work_slot_invocation(
        runner,
        compose_project="awf_ws_123",
        compose_file=_COMPOSE_FILE,
        invocation_id="wsl_abc123",
    )

    assert result == expected
    assert runner.calls[0].args[-1] == "wsl_abc123"
    assert runner.calls[0].timeout_seconds == compose_exec.WORK_SLOT_PROBE_TIMEOUT_SECONDS


async def test_probe_hung_exec_times_out_as_unknown() -> None:
    # A wedged ``docker compose exec`` must not freeze the worker loop that awaits
    # this probe; the deadline expiring is "could not tell", so the lease is kept.
    runner = FakeCommandRunner()
    runner.queue_hang()

    result = await probe_work_slot_invocation(
        runner,
        compose_project="awf_ws_123",
        compose_file=_COMPOSE_FILE,
        invocation_id="wsl_abc123",
        timeout_seconds=0.01,
    )

    assert result == "unknown"
    assert runner.calls[0].timeout_seconds == 0.01


async def test_probe_script_detects_a_live_tagged_process_and_its_absence() -> None:
    # Run the real probe scan against a real local process tagged the same way
    # ``-e AWF_WORK_SLOT_INVOCATION_ID=...`` tags an ``awf-exec`` descendant.
    invocation_id = f"wsl_probe_{os.getpid()}"
    script = compose_exec._work_slot_probe_script(invocation_id)  # noqa: SLF001
    runner = AsyncioSubprocessRunner()
    env = {**os.environ, WORK_SLOT_INVOCATION_ENV: invocation_id}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        env=env,
    )
    try:
        present = await runner.run(["sh", "-c", script, "awf-work-slot-probe", invocation_id])
    finally:
        process.kill()
        await process.wait()
    absent = await runner.run(["sh", "-c", script, "awf-work-slot-probe", invocation_id])

    assert present.returncode == 0
    assert present.stdout.strip() == f"awf work-slot probe: present {invocation_id}"
    assert absent.returncode == 0
    assert absent.stdout.strip() == f"awf work-slot probe: absent {invocation_id}"


_NON_DUMPABLE_SLEEPER = """
import ctypes, sys, time
ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0
print("ready", flush=True)
time.sleep(30)
"""


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() == 0,
    reason="needs Linux /proc and a non-root probe user",
)
async def test_probe_script_is_unknown_when_a_same_uid_environ_is_unreadable() -> None:
    # A non-dumpable tagged process hides its environ even from its own uid; the
    # scan cannot prove it gone, so it must not report ``absent``.
    invocation_id = f"wsl_unreadable_{os.getpid()}"
    script = compose_exec._work_slot_probe_script(invocation_id)  # noqa: SLF001
    env = {**os.environ, WORK_SLOT_INVOCATION_ENV: invocation_id}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _NON_DUMPABLE_SLEEPER,
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert (await process.stdout.readline()).strip() == b"ready"
        result = await AsyncioSubprocessRunner().run(
            ["sh", "-c", script, "awf-work-slot-probe", invocation_id]
        )
    finally:
        process.kill()
        await process.wait()

    assert result.returncode != 0
    assert result.stdout.strip().startswith(f"awf work-slot probe: unknown {invocation_id}")


async def test_probe_script_is_unknown_when_the_proc_walk_cannot_see_itself(
    tmp_path: Path,
) -> None:
    # An empty or hidden walk (no /proc, hidepid, foreign pid namespace) proves
    # nothing, so it must not report ``absent``.
    (tmp_path / "proc").mkdir()
    invocation_id = "wsl_empty_walk"
    script = compose_exec._work_slot_probe_script(invocation_id).replace(  # noqa: SLF001
        "/proc/", f"{tmp_path}/proc/"
    )

    result = await AsyncioSubprocessRunner().run(
        ["sh", "-c", script, "awf-work-slot-probe", invocation_id]
    )

    assert result.returncode != 0
    assert result.stdout.strip().startswith(f"awf work-slot probe: unknown {invocation_id}")
