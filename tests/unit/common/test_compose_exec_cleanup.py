"""Tracked docker compose exec command construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import (
    ComposeExecCleanupError,
    build_cleanup_compose_exec,
    build_tracked_compose_exec,
    cleanup_compose_exec_invocation,
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
