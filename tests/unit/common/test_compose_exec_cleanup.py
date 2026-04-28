"""Tracked docker compose exec command construction."""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.common.compose_exec as compose_exec
from awf.common.commands import CommandResult, FakeCommandRunner
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
        "--project-name",
        "awf_ws_123",
        "--file",
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
        "--project-name",
        "awf_ws_123",
        "--file",
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
