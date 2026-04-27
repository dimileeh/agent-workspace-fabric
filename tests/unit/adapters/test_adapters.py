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
from pathlib import Path
from typing import Any

import pytest
import structlog

# Importing the registry module forces adapter self-registration.
import awf.adapters.registry  # noqa: F401
from awf.adapters import get_adapter  # noqa: F401 - populates registry via __init__
from awf.adapters.base import AgentRunError
from awf.adapters.claude_code import ClaudeCodeAdapter, _claude_effort_for_awf_effort
from awf.adapters.codex import CodexAdapter
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS
from awf.adapters.gemini import GeminiAdapter
from awf.adapters.gemini import _settings_for_effort as gemini_settings_for_effort
from awf.adapters.gemini import _thinking_level_for_effort as gemini_thinking_level_for_effort
from awf.adapters.opencode import (
    OPENCODE_OLLAMA_CLOUD_MODELS,
    OpenCodeAdapter,
    _opencode_config_for_effort,
    _qualified_model,
    _thinking_enabled,
    _variant_for_effort,
)
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime

_PROMPT = "Add a one-line docstring to src/module/__init__.py."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")


def _assert_docker_exec_prefix(args: list[str]) -> None:
    """Common assertions for the docker compose exec prefix."""
    assert args[:2] == ["docker", "compose"]
    assert "--project-name" in args and _COMPOSE_PROJECT in args
    assert "--file" in args and str(_COMPOSE_FILE) in args
    exec_idx = args.index("exec")
    assert args[exec_idx : exec_idx + 4] == ["exec", "-T", "-w", "/workspace"]
    assert "agent" in args


class _TimeoutStreamingRunner:
    def __init__(self, *, reason_code: str) -> None:
        self.reason_code = reason_code
        self.used_streaming = False
        self.wall_timeout_seconds: float | None = None
        self.idle_timeout_seconds: float | None = None
        self.cleanup_calls: list[list[str]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
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
    def __init__(self) -> None:
        self.stdout_data: list[str] = []
        self.stderr_data: list[str] = []
        self.closed = False

    async def write_stdout(self, data: str) -> None:
        self.stdout_data.append(data)

    async def write_stderr(self, data: str) -> None:
        self.stderr_data.append(data)

    async def close(self) -> None:
        self.closed = True


class _RecordingLogStore:
    def __init__(self) -> None:
        self.sinks = _RecordingSinks()

    async def open_command_streams(self, **_kwargs: Any) -> _RecordingSinks:
        return self.sinks


class _RunOnlyRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append({"args": args, "input_bytes": input_bytes, "cwd": cwd})
        return CommandResult(returncode=0, stdout="legacy stdout", stderr="legacy stderr")


class _CancellingStreamingRunner:
    def __init__(self) -> None:
        self.cleanup_calls: list[list[str]] = []

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        raise asyncio.CancelledError


class _SlowCleanupAfterCancelRunner:
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
        raise asyncio.CancelledError


class TestCodexAdapter:
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
        # AWF prepends a contract preamble ("do not switch branches")
        # before the user-supplied prompt; the last argv element is
        # therefore the wrapped form. Check the user prompt is the
        # trailing substring so the assertion survives preamble edits.
        assert args[-1].endswith(_PROMPT)
        assert "AWF workspace contract" in args[-1]
        assert "--model" in args and "gpt-5" in args
        assert "-c" in args
        assert 'model_reasoning_effort="xhigh"' in args

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
    async def test_closes_stdin_for_noninteractive_exec(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        assert runner.calls[0].input_bytes == b""

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
        assert runner.calls[0]["input_bytes"] == b""
        assert log_store.sinks.stdout_data == ["legacy stdout"]
        assert log_store.sinks.stderr_data == ["legacy stderr"]
        assert log_store.sinks.closed is True


class TestClaudeCodeAdapter:
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
        # -p signals non-interactive print mode.
        assert args[-2] == "-p"
        # AWF prepends a contract preamble ("do not switch branches")
        # before the user-supplied prompt; the last argv element is
        # therefore the wrapped form. Check the user prompt is the
        # trailing substring so the assertion survives preamble edits.
        assert args[-1].endswith(_PROMPT)
        assert "AWF workspace contract" in args[-1]
        assert "--model" in args and "sonnet" in args
        assert "--effort" in args and "max" in args

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
    def test_effort_mapper_preserves_non_top_effort_values(self) -> None:
        assert _claude_effort_for_awf_effort("low") == "low"


class TestGeminiAdapter:
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
        assert args[-2] == "-p"
        # AWF prepends a contract preamble ("do not switch branches")
        # before the user-supplied prompt; the last argv element is
        # therefore the wrapped form. Check the user prompt is the
        # trailing substring so the assertion survives preamble edits.
        assert args[-1].endswith(_PROMPT)
        assert "AWF workspace contract" in args[-1]
        assert "-m" in args and "gemini-2.5-pro" in args

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
        assert "-m" not in args

    @pytest.mark.unit
    async def test_xhigh_effort_uses_system_settings_wrapper(self) -> None:
        runner = FakeCommandRunner()
        adapter = GeminiAdapter(
            runner=runner,
            default_model="gemini-3-pro-preview",
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
        assert "-m" in args and "gemini-3-pro-preview" in args
        assert args[-1].endswith(_PROMPT)

    @pytest.mark.unit
    def test_gemini_effort_helpers_map_only_high_effort_to_high_thinking(self) -> None:
        assert gemini_thinking_level_for_effort("high") == "HIGH"
        assert gemini_thinking_level_for_effort("xhigh") == "HIGH"
        assert gemini_thinking_level_for_effort("max") == "HIGH"
        assert gemini_thinking_level_for_effort("medium") is None
        assert gemini_settings_for_effort(model="gemini-3-pro-preview", effort="low") == {}
        no_model_settings = gemini_settings_for_effort(model=None, effort="xhigh")
        override = no_model_settings["modelConfigs"]["overrides"][0]  # type: ignore[index]
        assert override["match"] == {}
        assert (
            override["modelConfig"]["generateContentConfig"]["thinkingConfig"][
                "thinkingLevel"
            ]
            == "HIGH"
        )


class TestOpenCodeAdapter:
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
        assert args[sh_start : sh_start + 3] == ["sh", "-lc", args[sh_start + 2]]
        script = args[sh_start + 2]
        assert "OPENCODE_CONFIG_CONTENT" in script
        assert "AWF_OPENCODE_OLLAMA_BASE_URL" in script
        assert "host.docker.internal:11434/v1" in script
        assert "exec opencode run" in script
        assert '"permission":"allow"' in script
        assert '"think":true' in script
        assert model in script
        assert "--dangerously-skip-permissions" in args
        assert "--model" in args
        assert f"ollama/{model}" in args
        assert "--variant" in args
        assert "max" in args
        assert "--thinking" in args
        assert args[-1].endswith(_PROMPT)
        assert "AWF workspace contract" in args[-1]

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


class TestCentralDefaults:
    @pytest.mark.unit
    def test_defaults_map_uses_requested_models_and_xhigh_effort(self) -> None:
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.claude_code].model == "claude-opus-4-7"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.codex].model == "gpt-5.5"
        assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.gemini].model == "gemini-3-pro-preview"
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
    @pytest.mark.unit
    def test_all_adapters_registered(self) -> None:
        runner = FakeCommandRunner()

        codex = get_adapter(AgentRuntime.codex, runner=runner)
        claude = get_adapter(AgentRuntime.claude_code, runner=runner)
        gemini = get_adapter(AgentRuntime.gemini, runner=runner)
        opencode = get_adapter(AgentRuntime.opencode, runner=runner)

        assert codex.name == AgentRuntime.codex
        assert claude.name == AgentRuntime.claude_code
        assert gemini.name == AgentRuntime.gemini
        assert opencode.name == AgentRuntime.opencode
