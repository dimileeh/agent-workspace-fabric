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
