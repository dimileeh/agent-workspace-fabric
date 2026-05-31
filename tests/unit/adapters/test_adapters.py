"""Adapter tests — no real docker, no real CLI.

Each test runs the adapter against a FakeCommandRunner that records the
argv it's handed and returns canned output. We verify:

1. The adapter produces the right ``docker compose exec`` invocation.
2. The CLI-specific flags match the reference pattern for each CLI.
3. Non-zero exit → AgentRunError with the agent name and full result.
4. The registry populates correctly on import.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from pathlib import Path
from typing import Any

import pytest
import structlog

# Importing the registry module forces adapter self-registration.
import awf.adapters.registry  # noqa: F401
from awf.adapters import get_adapter  # noqa: F401 - populates registry via __init__
from awf.adapters.base import AgentAdapter, AgentRunError
from awf.adapters.claude_code import ClaudeCodeAdapter, _claude_effort_for_awf_effort
from awf.adapters.codex import CodexAdapter
from awf.adapters.cursor import CursorAdapter, _cursor_model_for_effort
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.gemini import GeminiAdapter
from awf.adapters.gemini import _settings_for_effort as gemini_settings_for_effort
from awf.adapters.gemini import _thinking_level_for_effort as gemini_thinking_level_for_effort
from awf.adapters.opencode import (
    OPENCODE_OLLAMA_CLOUD_MODELS,
    OpenCodeAdapter,
    _opencode_config_for_effort,
    _opencode_launcher_script,
    _qualified_model,
    _thinking_enabled,
    _variant_for_effort,
)
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime

_PROMPT = "Add a one-line docstring to src/module/__init__.py."
_LONG_PROMPT = "Review this oversized PR comment.\n" + ("x" * 140_000)
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")


def _assert_docker_exec_prefix(args: list[str]) -> None:
    """Common assertions for the docker compose exec prefix."""
    assert args[:2] == ["docker", "compose"]
    assert "-p" in args and _COMPOSE_PROJECT in args
    assert "-f" in args and str(_COMPOSE_FILE) in args
    exec_idx = args.index("exec")
    assert args[exec_idx : exec_idx + 4] == ["exec", "-T", "-w", "/workspace"]
    assert "agent" in args


def _assert_prompt_sent_on_stdin(runner: FakeCommandRunner, prompt: str = _PROMPT) -> str:
    input_bytes = runner.calls[0].input_bytes
    assert input_bytes is not None
    wrapped_prompt = input_bytes.decode()
    assert wrapped_prompt.endswith(prompt)
    assert "AWF workspace contract" in wrapped_prompt
    assert "DO NOT run AWF/GitHub-owned broad validation" in wrapped_prompt
    assert "inside the agent" in wrapped_prompt
    assert "pytest --cov" in wrapped_prompt
    assert "focused checks" in wrapped_prompt
    return wrapped_prompt


def _assert_prompt_not_in_argv(args: list[str], prompt: str = _PROMPT) -> None:
    assert all(prompt not in arg for arg in args)


class _TimeoutStreamingRunner:
    """Fake runner that simulates watchdog timeout and captures cleanup calls."""

    def __init__(self, *, reason_code: str) -> None:
        """Store expected timeout reason and initialize runner state."""
        self.reason_code = reason_code
        self.used_streaming = False
        self.wall_timeout_seconds: float | None = None
        self.idle_timeout_seconds: float | None = None
        self.cleanup_calls: list[list[str]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
        """Pretend to run cleanup and return a zero exit code."""
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        *,
        on_stdout: Any = None,
        on_stderr: Any = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Simulate a timed-out streaming run with watchdog metadata."""
        del input_bytes, cwd
        self.used_streaming = True
        self.wall_timeout_seconds = wall_timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        if on_stdout is not None:
            await on_stdout("partial work\n")
        if on_stderr is not None:
            await on_stderr("watchdog fired\n")
        return CommandResult(
            returncode=124,
            stdout="partial work\n",
            stderr="watchdog fired\n",
            reason_code=self.reason_code,
        )


class _RecordingSinks:
    """In-memory stdout/stderr sinks used by adapter log-store tests."""

    def __init__(self) -> None:
        """Initialize recorded stream buffers."""
        self.stdout_data: list[str] = []
        self.stderr_data: list[str] = []
        self.closed = False

    async def write_stdout(self, data: str) -> None:
        """Record stdout stream data."""
        self.stdout_data.append(data)

    async def write_stderr(self, data: str) -> None:
        """Record stderr stream data."""
        self.stderr_data.append(data)

    async def close(self) -> None:
        """Mark the sinks as closed."""
        self.closed = True


class _RecordingLogStore:
    """Log store stub that provides deterministic in-memory sinks."""

    def __init__(self) -> None:
        """Initialise a shared in-memory sink."""
        self.sinks = _RecordingSinks()

    async def open_command_streams(self, **_kwargs: Any) -> _RecordingSinks:
        """Return the in-memory sink used by adapter log stream assertions."""
        return self.sinks


class _RunOnlyRunner:
    """Runner that exercises the sync-only adapter execution path."""

    def __init__(self) -> None:
        """Initialize captured call history."""
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Record one call and return legacy success output."""
        self.calls.append({"args": args, "input_bytes": input_bytes, "cwd": cwd})
        return CommandResult(returncode=0, stdout="legacy stdout", stderr="legacy stderr")


class _CancellingStreamingRunner:
    """Runner that injects cancellation to test adapter cleanup behavior."""

    def __init__(self) -> None:
        """Initialize cleanup coordination events."""
        self.cleanup_calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Record cleanup call details and return success payload."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Reject streaming runs by raising cancellation for cleanup assertions."""
        raise asyncio.CancelledError


class _SlowCleanupAfterCancelRunner:
    """Runner that blocks cleanup briefly to test cancellation timing."""

    def __init__(self) -> None:
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.cleanup_calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Capture cleanup-callback details before cancellation."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        self.cleanup_finished.set()
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Raise cancellation immediately to force exception handling paths."""
        raise asyncio.CancelledError


class TestCodexAdapter:
    """End-to-end Codex adapter contract tests."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("wall_timeout", "idle_timeout", "message"),
        [
            (0.0, 1.0, "agent_wall_timeout_seconds must be positive"),
            (1.0, 0.0, "agent_idle_timeout_seconds must be positive"),
        ],
    )
    def test_rejects_non_positive_agent_timeouts(
        self,
        wall_timeout: float,
        idle_timeout: float,
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            CodexAdapter(
                runner=FakeCommandRunner(),
                agent_wall_timeout_seconds=wall_timeout,
                agent_idle_timeout_seconds=idle_timeout,
            )

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner, default_model="gpt-5", default_effort="xhigh")

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )
        assert result.ok
        assert len(runner.calls) == 1

        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)

        # Last args must be the codex-specific slice.
        codex_start = args.index("codex")
        assert args[codex_start : codex_start + 3] == [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        assert args[-1] == "-"
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)
        assert "--model" in args and "gpt-5" in args
        assert "-c" in args
        assert 'model_reasoning_effort="xhigh"' in args

    @pytest.mark.unit
    async def test_large_prompt_uses_stdin_not_argv(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner, default_model="gpt-5", default_effort="xhigh")

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_LONG_PROMPT,
        )

        args = runner.calls[0].args
        assert max(len(arg) for arg in args) < 10_000
        assert all(_LONG_PROMPT not in arg for arg in args)
        _assert_prompt_sent_on_stdin(runner, _LONG_PROMPT)

    @pytest.mark.unit
    async def test_no_model_omits_model_flags(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner, default_model=None)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )
        args = runner.calls[0].args
        assert "--model" not in args

    @pytest.mark.unit
    async def test_nonzero_exit_raises_agent_run_error(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=2, stderr="codex: auth required")
        adapter = CodexAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
            )
        assert exc.value.agent == AgentRuntime.codex
        assert exc.value.result.returncode == 2
        assert "codex: auth required" in exc.value.result.stderr

    @pytest.mark.unit
    async def test_streams_prompt_on_stdin_for_noninteractive_exec(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_wall_timeout_raises_structured_error_and_closes_log_streams(self) -> None:
        runner = _TimeoutStreamingRunner(reason_code="COMMAND_TIMEOUT")
        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
            agent_wall_timeout_seconds=12.0,
            agent_idle_timeout_seconds=3.0,
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(AgentRunError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout",
            )

        assert exc.value.reason_code == "AGENT_TIMEOUT"
        assert exc.value.result.stdout == "partial work\n"
        assert runner.wall_timeout_seconds == 12.0
        assert runner.idle_timeout_seconds == 3.0
        assert len(runner.cleanup_calls) == 1
        assert log_store.sinks.stdout_data == ["partial work\n"]
        assert log_store.sinks.stderr_data == ["watchdog fired\n"]
        assert log_store.sinks.closed is True
        assert any(
            event.get("event") == "agent.run.timeout"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_timeout"
            for event in captured
        )

    @pytest.mark.unit
    async def test_idle_timeout_uses_streaming_runner_even_without_log_store(self) -> None:
        runner = _TimeoutStreamingRunner(reason_code="COMMAND_IDLE_TIMEOUT")
        adapter = CodexAdapter(
            runner=runner,
            agent_wall_timeout_seconds=12.0,
            agent_idle_timeout_seconds=3.0,
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
            )

        assert exc.value.reason_code == "AGENT_IDLE_TIMEOUT"
        assert runner.used_streaming is True
        assert runner.wall_timeout_seconds == 12.0
        assert runner.idle_timeout_seconds == 3.0
        assert len(runner.cleanup_calls) == 1

    @pytest.mark.unit
    async def test_timeout_invokes_targeted_in_container_cleanup(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command idle timeout",
            reason_code="COMMAND_IDLE_TIMEOUT",
        )
        runner.queue_result(returncode=0, stdout="cleanup ok")
        adapter = CodexAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_agent_timeout",
            )

        assert exc.value.reason_code == "AGENT_IDLE_TIMEOUT"
        assert len(runner.calls) == 2
        agent_args = runner.calls[0].args
        cleanup_args = runner.calls[1].args
        invocation_id = agent_args[agent_args.index("awf-exec") + 1]
        assert cleanup_args[-1] == invocation_id
        assert cleanup_args[cleanup_args.index("exec") : cleanup_args.index("exec") + 5] == [
            "exec",
            "-T",
            "-w",
            "/workspace",
            "agent",
        ]
        assert "AWF_EXEC_INVOCATION_ID" in agent_args[agent_args.index("-lc") + 1]
        assert "pkill codex" not in cleanup_args[cleanup_args.index("-lc") + 1]
        assert "pkill claude" not in cleanup_args[cleanup_args.index("-lc") + 1]

    @pytest.mark.unit
    async def test_cleanup_failure_surfaces_distinct_error(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )
        runner.queue_result(returncode=1, stderr="tagged process still alive")
        adapter = CodexAdapter(runner=runner)

        with pytest.raises(ComposeExecCleanupError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cleanup_failed",
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert "tagged process still alive" in str(exc.value)
        assert len(runner.calls) == 2

    @pytest.mark.unit
    async def test_successful_agent_run_does_not_invoke_cleanup(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="done")
        adapter = CodexAdapter(runner=runner)

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_agent_success",
        )

        assert result.ok
        assert len(runner.calls) == 1
        assert "awf-cleanup" not in runner.calls[0].args

    @pytest.mark.unit
    async def test_cancelled_agent_run_cleans_up_in_container_invocation(self) -> None:
        runner = _CancellingStreamingRunner()
        adapter = CodexAdapter(runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cancelled_agent",
            )

        assert len(runner.cleanup_calls) == 1
        assert runner.cleanup_calls[0][-2] == "awf-cleanup"

    @pytest.mark.unit
    async def test_cancelled_agent_run_waits_for_cleanup_under_second_cancellation(self) -> None:
        runner = _SlowCleanupAfterCancelRunner()
        adapter = CodexAdapter(runner=runner)

        task = asyncio.create_task(
            adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cancelled_agent",
            )
        )
        await runner.cleanup_started.wait()

        task.cancel()
        await asyncio.sleep(0)

        try:
            assert not task.done()
        finally:
            runner.allow_cleanup.set()
            await runner.cleanup_finished.wait()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @pytest.mark.unit
    async def test_run_only_runner_falls_back_and_still_writes_log_streams(self) -> None:
        runner = _RunOnlyRunner()
        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            log_store=log_store,  # type: ignore[arg-type]
        )

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_legacy_runner",
        )

        assert result.stdout == "legacy stdout"
        assert result.stderr == "legacy stderr"
        assert runner.calls[0]["input_bytes"] is not None
        assert _PROMPT.encode() in runner.calls[0]["input_bytes"]
        assert log_store.sinks.stdout_data == ["legacy stdout"]
        assert log_store.sinks.stderr_data == ["legacy stderr"]
        assert log_store.sinks.closed is True


class TestClaudeCodeAdapter:
    """Claude adapter contract tests."""

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = ClaudeCodeAdapter(
            runner=runner,
            default_model="sonnet",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )
        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)

        claude_start = args.index("claude")
        assert args[claude_start : claude_start + 2] == [
            "claude",
            "--dangerously-skip-permissions",
        ]
        # -p signals non-interactive print mode; AWF streams the prompt
        # on stdin so large review comments never become one argv item.
        assert args[-1] == "-p"
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)
        assert "--model" in args and "sonnet" in args
        assert "--effort" in args and "xhigh" in args

    @pytest.mark.unit
    async def test_auth_failure_gets_structured_reason_code(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="Not logged in · Please run /login")
        adapter = ClaudeCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
            )

        assert exc.value.reason_code == "AGENT_AUTH_FAILED"

    @pytest.mark.unit
    async def test_capacity_exhausted_gets_structured_reason_code(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="RESOURCE_EXHAUSTED: quota exceeded")
        adapter = ClaudeCodeAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                model="claude-3-5-sonnet",
            )

        assert exc.value.reason_code == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
        assert exc.value.details["provider"] == "anthropic"
        assert exc.value.details["model"] == "claude-3-5-sonnet"
        assert exc.value.details["retryable"] is True
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
        assert provider_recovery["failure_type"] == "quota"
        assert provider_recovery["provider"] == "anthropic"
        assert provider_recovery["model"] == "claude-3-5-sonnet"

    @pytest.mark.unit
    async def test_codex_usage_limit_gets_structured_capacity_reason_code(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stderr=(
                "ERROR: You've hit your usage limit for GPT-5.3-Codex-Spark. "
                "Switch to another model now, or try again at 10:29 PM."
            ),
        )
        adapter = CodexAdapter(runner=runner)

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                model="gpt-5.3-codex-spark",
            )

        assert exc.value.reason_code == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
        assert exc.value.details["provider"] == "openai"
        assert exc.value.details["model"] == "gpt-5.3-codex-spark"
        assert exc.value.details["retryable"] is True
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
        assert provider_recovery["failure_type"] == "usage_limit"
        assert provider_recovery["provider"] == "openai"
        assert provider_recovery["model"] == "gpt-5.3-codex-spark"

    @pytest.mark.unit
    def test_effort_mapper_propagates_effort_unchanged_to_claude_cli(self) -> None:
        # The claude CLI accepts low/medium/high/xhigh/max natively, so AWF
        # propagates the requested effort as-is. xhigh must NOT collapse to max.
        assert _claude_effort_for_awf_effort("low") == "low"
        assert _claude_effort_for_awf_effort("medium") == "medium"
        assert _claude_effort_for_awf_effort("high") == "high"
        assert _claude_effort_for_awf_effort("xhigh") == "xhigh"
        assert _claude_effort_for_awf_effort("max") == "max"
        # Mixed-case input is normalized to lowercase, never remapped.
        assert _claude_effort_for_awf_effort("XHigh") == "xhigh"
        assert _claude_effort_for_awf_effort("MAX") == "max"


class TestGeminiAdapter:
    """Gemini adapter contract tests."""

    @pytest.mark.unit
    def test_reports_google_provider(self) -> None:
        adapter = GeminiAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("gemini-3.1-pro-preview") == "google"

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = GeminiAdapter(runner=runner, default_model="gemini-2.5-pro")

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )
        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)

        gemini_start = args.index("gemini")
        assert args[gemini_start : gemini_start + 3] == [
            "gemini",
            "--skip-trust",
            "--yolo",
        ]
        gemini_args = args[gemini_start:]
        assert gemini_args[3:5] == ["-p", ""]
        assert "--prompt" not in gemini_args
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)
        assert "--model" in args and "gemini-2.5-pro" in args

    @pytest.mark.unit
    async def test_produces_cli_invocation_without_model_or_effort(self) -> None:
        runner = FakeCommandRunner()
        adapter = GeminiAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        gemini_start = args.index("gemini")
        assert args[gemini_start : gemini_start + 3] == [
            "gemini",
            "--skip-trust",
            "--yolo",
        ]
        assert args[gemini_start + 3 : gemini_start + 5] == ["-p", ""]
        assert "--model" not in args

    @pytest.mark.unit
    async def test_xhigh_effort_uses_system_settings_wrapper(self) -> None:
        runner = FakeCommandRunner()
        adapter = GeminiAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-preview",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )
        args = runner.calls[0].args
        sh_start = [i for i, arg in enumerate(args) if arg == "sh"][-1]
        assert args[sh_start : sh_start + 3] == ["sh", "-lc", args[sh_start + 2]]
        script = args[sh_start + 2]
        assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in script
        assert '"thinkingLevel":"HIGH"' in script
        assert "GEMINI_CLI_TRUST_WORKSPACE" in script
        assert "exec gemini" in script
        assert "--model" in args and "gemini-3.1-pro-preview" in args
        gemini_args = args[sh_start:]
        assert "-p" in gemini_args
        assert gemini_args[gemini_args.index("-p") + 1] == ""
        assert "--prompt" not in gemini_args
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    def test_gemini_effort_helpers_map_only_high_effort_to_high_thinking(self) -> None:
        assert gemini_thinking_level_for_effort("high") == "HIGH"
        assert gemini_thinking_level_for_effort("xhigh") == "HIGH"
        assert gemini_thinking_level_for_effort("max") == "HIGH"
        assert gemini_thinking_level_for_effort("medium") is None
        assert gemini_settings_for_effort(model="gemini-3.1-pro-preview", effort="low") == {}
        no_model_settings = gemini_settings_for_effort(model=None, effort="xhigh")
        override = no_model_settings["modelConfigs"]["overrides"][0]  # type: ignore[index]
        assert override["match"] == {}
        assert (
            override["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"]
            == "HIGH"
        )


class TestOpenCodeAdapter:
    """OpenCode adapter contract tests."""

    @pytest.mark.unit
    def test_reports_provider_from_selected_or_default_model(self) -> None:
        default_adapter = OpenCodeAdapter(runner=FakeCommandRunner())
        openai_adapter = OpenCodeAdapter(
            runner=FakeCommandRunner(),
            default_model="openai/gpt-oss",
        )

        assert default_adapter.get_provider(None) == "ollama"
        assert default_adapter.get_provider("ollama/glm-5.1:cloud") == "ollama"
        assert openai_adapter.get_provider(None) == "openai"
        assert openai_adapter.get_provider("anthropic/claude-sonnet") == "anthropic"

    @pytest.mark.unit
    @pytest.mark.parametrize("model", OPENCODE_OLLAMA_CLOUD_MODELS)
    async def test_runs_opencode_with_each_supported_ollama_cloud_model(
        self,
        model: str,
    ) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model=model,
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)
        sh_start = [i for i, arg in enumerate(args) if arg == "sh"][-1]
        assert args[sh_start : sh_start + 3] == ["sh", "-c", args[sh_start + 2]]
        script = args[sh_start + 2]
        assert "OPENCODE_CONFIG_CONTENT" in script
        assert "AWF_OPENCODE_OLLAMA_BASE_URL" in script
        assert "host.docker.internal:11434/v1" in script
        assert "opencode run" in script
        assert "mktemp" in script
        assert "/tmp/awf-opencode-prompt.md" not in script
        assert '--file "$prompt_path"' in script
        assert '"permission":"allow"' in script
        assert '"think":true' in script
        assert model in script
        assert "--dangerously-skip-permissions" in args
        assert "--model" in args
        assert f"ollama/{model}" in args
        assert "--variant" in args
        assert "max" in args
        assert "--thinking" in args
        assert "--file" not in args
        assert args[-1] == "Follow the instructions in the attached AWF prompt file exactly."
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_preserves_fully_qualified_model_name(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model="ollama/glm-5.1:cloud",
            default_effort="max",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        assert "--model" in args
        assert "ollama/glm-5.1:cloud" in args

    @pytest.mark.unit
    async def test_default_opencode_invocation_omits_variant_without_effort(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        assert "--model" in args
        assert "ollama/kimi-k2.6:cloud" in args
        assert "--variant" not in args
        assert "--thinking" not in args

    @pytest.mark.unit
    def test_opencode_effort_helpers_cover_default_and_high_paths(self) -> None:
        assert _qualified_model("kimi-k2.6:cloud") == "ollama/kimi-k2.6:cloud"
        assert _qualified_model("ollama/glm-5.1:cloud") == "ollama/glm-5.1:cloud"
        assert _thinking_enabled(None) is False
        assert _thinking_enabled("medium") is False
        assert _thinking_enabled("high") is True
        assert _variant_for_effort(None) is None
        assert _variant_for_effort("medium") is None
        assert _variant_for_effort("high") == "high"
        assert _variant_for_effort("xhigh") == "max"
        assert _variant_for_effort("max") == "max"

        low_config = _opencode_config_for_effort(effort=None)
        models = low_config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert all("options" not in model for model in models.values())

    @pytest.mark.unit
    async def test_opencode_launcher_forwards_termination_and_cleans_temp_files(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        tmp_dir = tmp_path / "tmp"
        bin_dir.mkdir()
        tmp_dir.mkdir()
        fake_opencode = bin_dir / "opencode"
        fake_started = tmp_path / "started"
        fake_signal = tmp_path / "signal"
        fake_prompt = tmp_path / "prompt-copy"
        fake_opencode.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "prompt_path=\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--file" ]; then\n'
            "    shift\n"
            '    prompt_path="$1"\n'
            "  fi\n"
            "  shift || true\n"
            "done\n"
            'cat "$prompt_path" > "$AWF_FAKE_PROMPT_COPY"\n'
            "trap 'printf TERM > \"$AWF_FAKE_SIGNAL\"; exit 143' TERM\n"
            'printf started > "$AWF_FAKE_STARTED"\n'
            "while :; do sleep 1; done\n"
        )
        fake_opencode.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "TMPDIR": str(tmp_dir),
                "AWF_FAKE_STARTED": str(fake_started),
                "AWF_FAKE_SIGNAL": str(fake_signal),
                "AWF_FAKE_PROMPT_COPY": str(fake_prompt),
            }
        )

        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _opencode_launcher_script(effort="xhigh"),
            "awf-opencode",
            "--model",
            "ollama/kimi-k2.6:cloud",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        for _ in range(250):
            if fake_started.exists():
                break
            await asyncio.sleep(0.02)
        assert fake_started.exists()

        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)

        assert proc.returncode == 143
        assert fake_signal.read_text() == "TERM"
        assert fake_prompt.read_text() == "workspace prompt"
        assert list(tmp_dir.glob("awf-opencode-prompt.*.md")) == []
        assert list(tmp_dir.glob("awf-opencode-config.*.json")) == []


class TestCursorAdapter:
    """Cursor adapter contract tests."""

    @pytest.mark.unit
    def test_reports_cursor_provider(self) -> None:
        """Cursor reports its own provider for model attribution."""
        adapter = CursorAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("sonnet-4-thinking") == "cursor"

    @pytest.mark.unit
    async def test_produces_correct_default_cli_invocation(self) -> None:
        """The default Cursor run uses print mode, force, and text output."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(
            runner=runner,
            default_model="sonnet-4-thinking",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)
        cursor_start = args.index("cursor-agent")
        assert args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "-m",
            "sonnet-4-thinking",
            "--output-format",
            "text",
        ]
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_model_override_is_passed_without_prompt_argv(self) -> None:
        """Explicit models are passed while prompts remain stdin-only."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(
            runner=runner,
            default_model="sonnet-4-thinking",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            model="gpt-5",
        )

        args = runner.calls[0].args
        cursor_start = args.index("cursor-agent")
        assert args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "-m",
            "gpt-5",
            "--output-format",
            "text",
        ]
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_no_model_omits_model_flag_but_keeps_force_and_text_output(self) -> None:
        """Cursor omits -m when no model or effort-derived model is selected."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        cursor_start = args.index("cursor-agent")
        assert args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "--output-format",
            "text",
        ]
        assert "-m" not in args

    @pytest.mark.unit
    def test_effort_mapping_uses_documented_models_not_extra_flags(self) -> None:
        """Effort mapping selects models instead of undocumented Cursor flags."""
        assert _cursor_model_for_effort(model="gpt-5", effort="xhigh") == "gpt-5"
        assert _cursor_model_for_effort(model="sonnet-4", effort="high") == "sonnet-4"
        assert _cursor_model_for_effort(model=None, effort=None) is None
        assert _cursor_model_for_effort(model=None, effort="medium") is None
        assert _cursor_model_for_effort(model=None, effort="high") == "sonnet-4-thinking"
        assert _cursor_model_for_effort(model=None, effort="xhigh") == "sonnet-4-thinking"
        assert _cursor_model_for_effort(model=None, effort="max") == "sonnet-4-thinking"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("adapter_cls", "runtime"),
    [
        (ClaudeCodeAdapter, AgentRuntime.claude_code),
        (CodexAdapter, AgentRuntime.codex),
        (CursorAdapter, AgentRuntime.cursor),
        (GeminiAdapter, AgentRuntime.gemini),
        (OpenCodeAdapter, AgentRuntime.opencode),
    ],
)
async def test_all_adapters_keep_oversized_prompts_out_of_argv(
    adapter_cls: type[AgentAdapter],
    runtime: AgentRuntime,
) -> None:
    runner = FakeCommandRunner()
    defaults = DEFAULT_AGENT_DEFAULTS[runtime]
    adapter = adapter_cls(
        runner=runner,
        default_model=defaults.model,
        default_effort=defaults.effort,
    )

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_LONG_PROMPT,
    )

    args = runner.calls[0].args
    assert max(len(arg) for arg in args) < 10_000
    assert all(_LONG_PROMPT not in arg for arg in args)
    _assert_prompt_sent_on_stdin(runner, _LONG_PROMPT)


@pytest.mark.unit
def test_adapter_cli_args_contract_excludes_prompt_payload() -> None:
    """Adapter CLI arg builders keep prompt payloads out of signatures."""
    for adapter_cls in (
        ClaudeCodeAdapter,
        CodexAdapter,
        CursorAdapter,
        GeminiAdapter,
        OpenCodeAdapter,
    ):
        assert "prompt" not in inspect.signature(adapter_cls._cli_args).parameters


class TestCentralDefaults:
    """Default model and effort mapping tests."""

    @pytest.mark.unit
    def test_defaults_map_uses_requested_models_and_xhigh_effort(self) -> None:
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.claude_code].model == "claude-opus-4-8"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex].model == "gpt-5.5"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.cursor].model == "sonnet-4-thinking"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.gemini].model == "gemini-3.1-pro-preview"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.opencode].model == "ollama/kimi-k2.6:cloud"
        assert {d.effort for d in DEFAULT_AGENT_DEFAULTS.values()} == {"xhigh"}

    @pytest.mark.unit
    def test_get_adapter_applies_full_defaults(self) -> None:
        runner = FakeCommandRunner()

        codex = get_adapter(
            AgentRuntime.codex,
            runner=runner,
            defaults=DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex],
        )

        assert codex._default_model == "gpt-5.5"
        assert codex._default_effort == "xhigh"


class TestRegistry:
    """Adapter registry wiring tests."""

    @pytest.mark.unit
    def test_all_adapters_registered(self) -> None:
        """Every supported runtime resolves through the central registry."""
        runner = FakeCommandRunner()

        codex = get_adapter(AgentRuntime.codex, runner=runner)
        claude = get_adapter(AgentRuntime.claude_code, runner=runner)
        cursor = get_adapter(AgentRuntime.cursor, runner=runner)
        gemini = get_adapter(AgentRuntime.gemini, runner=runner)
        opencode = get_adapter(AgentRuntime.opencode, runner=runner)

        assert codex.name == AgentRuntime.codex
        assert claude.name == AgentRuntime.claude_code
        assert cursor.name == AgentRuntime.cursor
        assert gemini.name == AgentRuntime.gemini
        assert opencode.name == AgentRuntime.opencode
