"""Tracked docker compose exec command construction."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import awf.common.compose_exec as compose_exec
from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.common.compose_exec import (
    ComposeExecCleanupError,
    build_cleanup_compose_exec,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
    cleanup_compose_exec_invocation_after_cancellation,
    cleanup_failure_message,
)


def test_builds_tracked_exec_wrapper_with_unique_invocation_id() -> None:
    invocation = build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=Path("/tmp/ws/compose.yml"),
        cli_args=["codex", "exec", "--model", "gpt-5", "fix bug"],
        source="agent",
        label="codex",
        invocation_id="awf_test_invocation",
    )

    args = invocation.args
    assert args[:8] == [
        "docker",
        "compose",
        "-p",
        "awf_ws_123",
        "-f",
        "/tmp/ws/compose.yml",
        "exec",
        "-T",
    ]
    exec_idx = args.index("exec")
    assert args[exec_idx : exec_idx + 5] == ["exec", "-T", "-w", "/workspace", "agent"]
    assert args[exec_idx + 5 : exec_idx + 8] == ["sh", "-lc", invocation.wrapper_script]
    assert "AWF_EXEC_INVOCATION_ID" in invocation.wrapper_script
    assert "pkill" not in invocation.wrapper_script
    assert "killall" not in invocation.wrapper_script
    assert args[exec_idx + 8 :] == [
        "awf-exec",
        "awf_test_invocation",
        "codex",
        "exec",
        "--model",
        "gpt-5",
        "fix bug",
    ]


def test_cleanup_command_targets_only_invocation_id() -> None:
    invocation = build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=Path("/tmp/ws/compose.yml"),
        cli_args=["claude", "--print", "fix bug"],
        source="agent",
        label="claude_code",
        invocation_id="awf_cleanup_target",
    )

    cleanup = build_cleanup_compose_exec(invocation)

    assert cleanup[:8] == [
        "docker",
        "compose",
        "-p",
        "awf_ws_123",
        "-f",
        "/tmp/ws/compose.yml",
        "exec",
        "-T",
    ]
    exec_idx = cleanup.index("exec")
    assert cleanup[exec_idx : exec_idx + 5] == ["exec", "-T", "-w", "/workspace", "agent"]
    assert cleanup[exec_idx + 5 : exec_idx + 8] == ["sh", "-lc", invocation.cleanup_script]
    assert cleanup[exec_idx + 8 :] == ["awf-cleanup", "awf_cleanup_target"]
    assert "AWF_EXEC_INVOCATION_ID=awf_cleanup_target" in invocation.cleanup_script
    forbidden = ("pkill claude", "pkill codex", "pkill pytest", "killall")
    assert all(marker not in invocation.cleanup_script for marker in forbidden)


def test_cleanup_command_bounds_integer_sleep_fallback_wait() -> None:
    invocation = build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=Path("/tmp/ws/compose.yml"),
        cli_args=["codex"],
        source="agent",
        label="codex",
        invocation_id="awf_cleanup_sleep",
    )

    script = invocation.cleanup_script

    assert "sleep 0.1 2>/dev/null || sleep 1" not in script
    assert "awf_cleanup_wait_limit=20" in script
    assert "awf_cleanup_wait_limit=2" in script


def test_preserved_stdin_setsid_path_uses_open_fd_not_path_redirection() -> None:
    script = compose_exec._tracked_exec_wrapper_script(preserve_stdin=True)  # noqa: SLF001
    setsid_block = script[script.index("if command -v setsid") : script.index('wait "$child_pid"')]

    assert 'exec 9< "$stdin_path"' in setsid_block
    assert 'setsid "$@" <&9 &' in setsid_block
    assert 'setsid "$@" < "$stdin_path" &' not in setsid_block
    assert setsid_block.index('rm -f "$stdin_path"') < setsid_block.index('setsid "$@" <&9 &')
    assert "exec 9<&-" in setsid_block


def test_default_stdin_setsid_path_does_not_touch_fd_9() -> None:
    script = compose_exec._tracked_exec_wrapper_script()  # noqa: SLF001
    setsid_block = script[script.index("if command -v setsid") : script.index('wait "$child_pid"')]

    assert 'exec 9< "$stdin_path"' not in setsid_block
    assert "exec 9<&-" not in setsid_block
    assert 'setsid "$@" </dev/null &' in setsid_block


def test_rejects_empty_or_unsafe_invocation_inputs() -> None:
    with pytest.raises(ValueError, match="cli_args"):
        build_tracked_compose_exec(
            compose_project="awf_ws_123",
            compose_file=Path("/tmp/ws/compose.yml"),
            cli_args=[],
            source="agent",
            label="codex",
        )

    with pytest.raises(ValueError, match="unsupported shell characters"):
        build_tracked_compose_exec(
            compose_project="awf_ws_123",
            compose_file=Path("/tmp/ws/compose.yml"),
            cli_args=["codex"],
            source="agent",
            label="codex",
            invocation_id="bad;id",
        )


@pytest.mark.unit
async def test_tracked_exec_wrapper_preserves_stdin_when_requested() -> None:
    stdin_path = Path("/tmp/awf-exec/awf_stdin_probe/stdin")
    script = compose_exec._tracked_exec_wrapper_script(preserve_stdin=True)  # noqa: SLF001
    result = await AsyncioSubprocessRunner().run(
        [
            "sh",
            "-lc",
            script,
            "awf-exec",
            "awf_stdin_probe",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read(), end='')",
        ],
        input_bytes=b"stdin-ok",
    )

    assert result.returncode == 0
    assert result.stdout == "stdin-ok"
    assert not stdin_path.exists()


@pytest.mark.unit
async def test_tracked_exec_wrapper_restricts_preserved_stdin_permissions() -> None:
    stdin_path = Path("/tmp/awf-exec/awf_stdin_permissions/stdin")
    child_path = Path("/tmp/awf-exec/awf_stdin_permissions/child-created")
    child_path.unlink(missing_ok=True)
    script = compose_exec._tracked_exec_wrapper_script(preserve_stdin=True)  # noqa: SLF001
    result = await AsyncioSubprocessRunner().run(
        [
            "sh",
            "-lc",
            script,
            "awf-exec",
            "awf_stdin_permissions",
            sys.executable,
            "-c",
            "import os, stat, sys; "
            "current_umask = os.umask(0); "
            "os.umask(current_umask); "
            "expected_child_mode = 0o666 & ~current_umask; "
            "open('/tmp/awf-exec/awf_stdin_permissions/child-created', 'w').close(); "
            "dir_mode = stat.S_IMODE(os.stat('/tmp/awf-exec/awf_stdin_permissions').st_mode); "
            "stdin_mode = stat.S_IMODE(os.fstat(0).st_mode); "
            "child_mode = stat.S_IMODE("
            "os.stat('/tmp/awf-exec/awf_stdin_permissions/child-created').st_mode"
            "); "
            "print("
            "f'{dir_mode:o} {stdin_mode:o} {child_mode:o} {expected_child_mode:o} "
            "{sys.stdin.read()}', "
            "end='',"
            ")",
        ],
        input_bytes=b"private-prompt",
    )

    assert result.returncode == 0
    dir_mode, stdin_mode, child_mode, expected_child_mode, prompt = result.stdout.split(" ", 4)
    assert (dir_mode, stdin_mode, child_mode, prompt) == (
        "700",
        "600",
        expected_child_mode,
        "private-prompt",
    )
    assert not stdin_path.exists()
    child_path.unlink(missing_ok=True)


@pytest.mark.unit
async def test_tracked_exec_wrapper_fails_when_preserved_stdin_cannot_be_spooled() -> None:
    blocked_path = Path("/tmp/awf-exec/awf_stdin_spool_blocked")
    blocked_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_path.write_text("not a directory", encoding="utf-8")
    try:
        script = compose_exec._tracked_exec_wrapper_script(preserve_stdin=True)  # noqa: SLF001
        result = await AsyncioSubprocessRunner().run(
            [
                "sh",
                "-lc",
                script,
                "awf-exec",
                "awf_stdin_spool_blocked",
                sys.executable,
                "-c",
                "print('should-not-run')",
            ],
            input_bytes=b"stdin-will-not-spool",
        )
    finally:
        blocked_path.unlink(missing_ok=True)

    assert result.returncode != 0
    assert "should-not-run" not in result.stdout


@pytest.mark.unit
async def test_tracked_exec_wrapper_closes_stdin_by_default() -> None:
    script = compose_exec._tracked_exec_wrapper_script()  # noqa: SLF001
    result = await AsyncioSubprocessRunner().run(
        [
            "sh",
            "-lc",
            script,
            "awf-exec",
            "awf_stdin_closed",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read(), end='')",
        ],
        input_bytes=b"should-not-reach-child",
    )

    assert result.returncode == 0
    assert result.stdout == ""


async def test_cleanup_success_accepts_killed_result() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="awf cleanup: killed awf_done\n")
    invocation = build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=Path("/tmp/ws/compose.yml"),
        cli_args=["codex"],
        source="agent",
        label="codex",
        invocation_id="awf_done",
    )

    result = await cleanup_compose_exec_invocation(runner, invocation, workspace_id="ws_123")

    assert result.ok
    assert runner.calls[0].args[-2:] == ["awf-cleanup", "awf_done"]


async def test_cleanup_after_cancellation_returns_completed_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="awf cleanup: absent\n")
    invocation = build_tracked_compose_exec(
        compose_project="awf_ws_123",
        compose_file=Path("/tmp/ws/compose.yml"),
        cli_args=["codex"],
        source="agent",
        label="codex",
        invocation_id="awf_cancel_done",
    )
    original_shield = compose_exec.asyncio.shield
    calls = 0

    async def shield_once(task: object) -> object:
        nonlocal calls
        calls += 1
        result = await original_shield(task)
        if calls == 1:
            raise compose_exec.asyncio.CancelledError
        return result

    monkeypatch.setattr(compose_exec.asyncio, "shield", shield_once)

    result = await cleanup_compose_exec_invocation_after_cancellation(
        runner,
        invocation,
        workspace_id="ws_123",
    )

    assert result.ok
    assert calls == 1


def test_cleanup_failure_message_is_bounded() -> None:
    exc = ComposeExecCleanupError(
        invocation_id="awf_long",
        source="validation",
        label="01_validate",
        message="x" * 3000,
        cleanup_result=CommandResult(returncode=1, stdout="", stderr=""),
    )

    message = cleanup_failure_message(exc)

    assert len(message) == 2000
    assert message.endswith("...")
