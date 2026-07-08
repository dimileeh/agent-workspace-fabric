"""Cloud-neutral runtime execution seam tests.

These guard the adapter dispatch between the default tracked
``docker compose exec`` path (when ``runtime_executor is None``) and an
injected ``AgentRuntimeExecutor`` (hosted path). They assert:

- Default Compose command construction is unchanged (regression guard).
- The injected executor is called for ``adapter.run`` and receives the
  prompt via ``prompt_stdin`` (bytes), not argv; ``cli_args`` excludes the
  prompt; ``env_passthrough_names`` carries names only, no values.
- Hosted result maps to ``AgentRunResult``; non-zero raises
  ``AgentRunError`` with the same provider-failure classification reason
  codes (timeout → ``AGENT_TIMEOUT``, auth → ``AGENT_AUTH_FAILED``).
- Secret values are never present in the request, argv, stdin, or captured
  logs.
- Usage sampling is skipped on the hosted path (the hosted executor owns
  its own runtime; sampling via ``docker compose exec`` is invalid there),
  while log-store streaming still runs.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
import structlog

import awf.adapters.registry  # noqa: F401 — populate registry
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.adapters.runtime_executor import (
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
)
from awf.common.commands import FakeCommandRunner
from awf.db.enums import AgentRuntime

_PROMPT = "Add a one-line docstring to src/module/__init__.py."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")
_SECRET_VALUE = "sk-test-secret-do-not-leak-1234567890"


class _RecordingExecutor:
    """Captures the single execute() call and returns a canned result."""

    def __init__(self, *, result: AgentRuntimeExecResult | None = None) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []
        self._result = result or AgentRuntimeExecResult(
            returncode=0, stdout="hosted stdout", stderr=""
        )

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        return self._result


class _StreamingExecutor:
    """A hosted executor that streams stdout/stderr chunks via the request's
    callbacks during ``execute()`` and returns a buffered result mirroring the
    streamed data, so the adapter's double-write guard can be exercised.
    """

    def __init__(self) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []
        self.stdout_chunks: list[str] = ["partial-1\n", "partial-2\n"]
        self.stderr_chunks: list[str] = ["warn-1\n"]

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        # Stream chunks live, the way a real streaming executor would.
        if request.on_stdout is not None:
            for chunk in self.stdout_chunks:
                maybe = request.on_stdout(chunk)
                if inspect.isawaitable(maybe):
                    await maybe
        if request.on_stderr is not None:
            for chunk in self.stderr_chunks:
                maybe = request.on_stderr(chunk)
                if inspect.isawaitable(maybe):
                    await maybe
        # Return the same content buffered — the adapter must NOT re-write it.
        return AgentRuntimeExecResult(
            returncode=0,
            stdout="".join(self.stdout_chunks),
            stderr="".join(self.stderr_chunks),
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
        self.open_calls: list[dict[str, Any]] = []

    async def open_command_streams(self, **kwargs: Any) -> _RecordingSinks:
        self.open_calls.append(dict(kwargs))
        return self.sinks


def _assert_docker_exec_prefix(args: list[str]) -> None:
    assert args[:2] == ["docker", "compose"]
    assert "-p" in args and _COMPOSE_PROJECT in args
    assert "-f" in args and str(_COMPOSE_FILE) in args
    exec_idx = args.index("exec")
    assert args[exec_idx : exec_idx + 4] == ["exec", "-T", "-w", "/workspace"]
    assert "agent" in args


class TestRuntimeExecutorSeam:
    @pytest.mark.unit
    async def test_default_compose_path_unchanged_when_executor_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``runtime_executor is None`` the Compose argv is byte-identical."""
        monkeypatch.setenv("OPENAI_API_KEY", _SECRET_VALUE)
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner, default_model="gpt-5", default_effort="xhigh")

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_default",
        )

        assert result.ok
        assert len(runner.calls) == 1
        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)
        codex_start = args.index("codex")
        assert args[codex_start : codex_start + 3] == [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        assert args[-1] == "-"
        # Prompt streamed on stdin, never in argv.
        assert all(_PROMPT not in arg for arg in args)
        input_bytes = runner.calls[0].input_bytes
        assert input_bytes is not None
        assert input_bytes.decode().endswith(_PROMPT)
        # Secret value never leaks into argv.
        assert not any(_SECRET_VALUE in arg for arg in args)

    @pytest.mark.unit
    async def test_injected_executor_receives_prompt_on_stdin_not_argv(self) -> None:
        executor = _RecordingExecutor()
        runner = FakeCommandRunner()  # must NOT be used on hosted path
        adapter = CodexAdapter(
            runner=runner,
            default_model="gpt-5",
            default_effort="xhigh",
            runtime_executor=executor,
        )

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_hosted",
        )

        assert result.ok
        assert result.stdout == "hosted stdout"
        assert runner.calls == [], "compose runner must not be used on hosted path"
        assert len(executor.calls) == 1
        request = executor.calls[0]

        # Prompt transported via stdin bytes, never argv.
        assert request.prompt_stdin.decode().endswith(_PROMPT)
        assert b"AWF workspace contract" in request.prompt_stdin
        assert all(_PROMPT not in arg for arg in request.cli_args)
        assert "-" in request.cli_args  # codex reads prompt from stdin

        # Structured context is secret-free.
        assert request.workspace_id == "ws_hosted"
        assert request.agent_runtime is AgentRuntime.codex
        assert request.log_source == "agent"
        assert request.model == "gpt-5"
        assert request.effort == "xhigh"
        assert request.wall_timeout_seconds is not None and request.wall_timeout_seconds > 0
        assert request.idle_timeout_seconds is not None and request.idle_timeout_seconds > 0

    @pytest.mark.unit
    async def test_env_passthrough_names_carry_names_only_no_values(self) -> None:
        executor = _RecordingExecutor()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_env",
        )

        request = executor.calls[0]
        # Codex hosted contract: CODEX_API_KEY name surfaced, no value.
        assert "CODEX_API_KEY" in request.env_passthrough_names
        # No secret values anywhere in the request payload.
        payload_blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + str(request.model or "")
        )
        assert _SECRET_VALUE not in payload_blob
        assert "sk-" not in payload_blob

    @pytest.mark.unit
    async def test_hosted_nonzero_exit_raises_agent_run_error_with_classification(
        self,
    ) -> None:
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=2, stdout="", stderr="codex: please set an auth method"
            )
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_fail",
            )

        assert exc.value.agent is AgentRuntime.codex
        assert exc.value.result.returncode == 2
        # Auth-failure classification mirrors the Compose path.
        assert exc.value.reason_code == "AGENT_AUTH_FAILED"
        assert exc.value.details is not None
        assert exc.value.details.get("retryable") is True

    @pytest.mark.unit
    async def test_hosted_timeout_exit_maps_to_agent_timeout(
        self,
    ) -> None:
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=124, stdout="partial", stderr="watchdog fired")
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout",
            )

        assert exc.value.reason_code == "AGENT_TIMEOUT"
        assert exc.value.result.returncode == 124

    @pytest.mark.unit
    async def test_hosted_idle_timeout_signal_maps_to_agent_idle_timeout(
        self,
    ) -> None:
        from awf.common.commands import COMMAND_IDLE_TIMEOUT_REASON

        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=124,
                stdout="partial",
                stderr="idle watchdog fired",
                timeout_reason=COMMAND_IDLE_TIMEOUT_REASON,
            )
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_idle_timeout",
            )

        assert exc.value.reason_code == "AGENT_IDLE_TIMEOUT"
        assert exc.value.result.returncode == 124

    @pytest.mark.unit
    async def test_hosted_124_without_explicit_timeout_reason_is_cli_failure(
        self,
    ) -> None:
        # Regression guard: a hosted ``124`` that does NOT carry an explicit,
        # valid timeout reason must NOT be misclassified as a wall-clock
        # timeout. The Compose path only reports ``AGENT_TIMEOUT`` when the
        # watchdog sets ``reason_code``; the hosted path mirrors that, so a
        # real CLI failure that happens to exit 124 (with an explicit non-
        # timeout ``timeout_reason``) classifies as an ordinary CLI failure
        # instead of triggering timeout recovery.
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=124,
                stdout="",
                stderr="cli exited 124 for its own reason",
                timeout_reason="",
            )
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_124_cli_failure",
            )

        assert exc.value.result.returncode == 124
        assert exc.value.reason_code == "AGENT_CLI_FAILED"

    @pytest.mark.unit
    async def test_hosted_path_streams_to_log_store(self) -> None:
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=0, stdout="line1\nline2\n", stderr="warn\n")
        )
        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            log_store=log_store,  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_logs",
        )

        assert log_store.sinks.stdout_data == ["line1\nline2\n"]
        assert log_store.sinks.stderr_data == ["warn\n"]
        assert log_store.sinks.closed is True

    @pytest.mark.unit
    async def test_hosted_streaming_executor_writes_live_chunks_not_double_written(self) -> None:
        # A streaming executor invokes the request's on_stdout/on_stderr
        # callbacks during execute() so the log store fills live, and the
        # adapter must NOT re-write the buffered result to the sinks (no
        # double-write). This is the regression guard for the review thread
        # that asked for live hosted log streaming.
        executor = _StreamingExecutor()
        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            log_store=log_store,  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        result = await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_stream",
        )

        assert result.ok
        # The request carried streaming callbacks (not None) so a streaming
        # executor can stream during execution.
        request = executor.calls[0]
        assert request.on_stdout is not None
        assert request.on_stderr is not None
        # Live chunks were streamed to the sinks during execution.
        assert log_store.sinks.stdout_data == ["partial-1\n", "partial-2\n"]
        assert log_store.sinks.stderr_data == ["warn-1\n"]
        # Buffered result was NOT re-written (no duplicate entries).
        assert len(log_store.sinks.stdout_data) == 2
        assert len(log_store.sinks.stderr_data) == 1
        assert log_store.sinks.closed is True

    @pytest.mark.unit
    async def test_hosted_non_streaming_executor_still_writes_buffered_result(self) -> None:
        # Backward-compat: an executor that does not invoke the streaming
        # callbacks still gets its buffered result written to the sinks after
        # execute() returns — the prior buffered-output contract is
        # preserved. (The request still carries callbacks so a future
        # streaming executor can use them; a non-streaming one simply ignores
        # them.)
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=0, stdout="buffered\n", stderr="e\n")
        )
        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            log_store=log_store,  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_no_stream",
        )

        # Buffered result written exactly once (no streaming, no double-write).
        assert log_store.sinks.stdout_data == ["buffered\n"]
        assert log_store.sinks.stderr_data == ["e\n"]
        assert log_store.sinks.closed is True

    @pytest.mark.unit
    async def test_hosted_path_does_not_log_secret_values(self) -> None:
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=0, stdout="ok", stderr="")
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            runtime_executor=executor,
        )

        with structlog.testing.capture_logs() as captured:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_secret_log",
            )

        blob = repr(captured)
        assert _SECRET_VALUE not in blob
        assert "sk-" not in blob

    @pytest.mark.unit
    async def test_hosted_usage_sampling_skipped(self) -> None:
        # The hosted executor owns its own runtime; the compose-based usage
        # sampler must NOT be started on the hosted path (it would build an
        # invalid ``docker compose -p "" -f "" exec`` invocation).
        starts: list[dict[str, Any]] = []

        class _StubSampler:
            async def start(self, **kwargs: Any) -> Any:
                starts.append(kwargs)
                raise AssertionError("usage sampler must not be started on hosted path")

        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            usage_sampler=_StubSampler(),  # type: ignore[arg-type]
            runtime_executor=_RecordingExecutor(),
        )

        async def _sentinel_start(**kwargs: Any) -> None:
            starts.append(kwargs)
            raise AssertionError("usage sampling must not start on hosted path")

        adapter._start_usage_sampling = _sentinel_start  # type: ignore[method-assign]

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_usage",
        )

        assert starts == [], "usage sampling must not start on hosted path"

    @pytest.mark.unit
    async def test_hosted_failure_secret_not_in_command_result_payload(self) -> None:
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=2, stdout="", stderr=f"err: {_SECRET_VALUE}")
        )
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            runtime_executor=executor,
        )

        with pytest.raises(AgentRunError):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_secret_err",
            )

        request = executor.calls[0]
        # The secret in stderr came from the hosted executor's result, not
        # from anything Core transported: the request Core built must still
        # be secret-free.
        request_blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
        )
        assert _SECRET_VALUE not in request_blob

    @pytest.mark.unit
    async def test_hosted_cancellation_closes_sinks_and_propagates(self) -> None:
        # A hosted run cancelled mid-execution must close the log-store sinks
        # in the ``finally`` block and propagate ``CancelledError``. This
        # exercises the hosted-path ``except asyncio.CancelledError`` handler
        # (``final_status = "cancelled"``) and the ``finally`` sink close.
        class _CancellingExecutor:
            async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
                raise asyncio.CancelledError

        log_store = _RecordingLogStore()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            log_store=log_store,  # type: ignore[arg-type]
            runtime_executor=_CancellingExecutor(),
        )

        with pytest.raises(asyncio.CancelledError):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cancelled_hosted",
            )

        # Sinks opened then closed by the finally block even on cancellation.
        assert len(log_store.open_calls) == 1
        assert log_store.sinks.closed is True


class TestAgentAdapterBaseDefaults:
    """Base ``AgentAdapter`` property defaults that concrete adapters inherit.

    These guard the extension contract: an adapter without an explicit default
    model reports ``None``, the hosted env-passthrough default is empty (so an
    adapter that does not override it surfaces no hosted credentials), and
    ``provider_recovery_default_model`` delegates to the model-selection hook.
    """

    @pytest.mark.unit
    def test_default_model_is_none_when_not_configured(self) -> None:
        adapter = CodexAdapter(runner=FakeCommandRunner())
        assert adapter.default_model is None

    @pytest.mark.unit
    def test_default_model_returns_configured_value(self) -> None:
        adapter = CodexAdapter(runner=FakeCommandRunner(), default_model="gpt-5")
        assert adapter.default_model == "gpt-5"

    @pytest.mark.unit
    def test_provider_recovery_default_model_delegates_to_model_selection(self) -> None:
        # With no explicit default model, the recovery identity is None (the
        # same value _selected_model_for_run returns for model=None).
        adapter = CodexAdapter(runner=FakeCommandRunner())
        assert adapter.provider_recovery_default_model is None
        adapter_with_default = CodexAdapter(runner=FakeCommandRunner(), default_model="gpt-5")
        assert adapter_with_default.provider_recovery_default_model == "gpt-5"

    @pytest.mark.unit
    async def test_base_hosted_env_passthrough_names_default_is_empty(self) -> None:
        # A minimal adapter subclass that does NOT override
        # ``hosted_env_passthrough_names`` exercises the base default (empty),
        # so a hosted run for such an adapter arrives with no passthrough names.
        from awf.adapters.base import AgentAdapter

        class _MinimalAdapter(AgentAdapter):
            @property
            def name(self) -> AgentRuntime:
                return AgentRuntime.codex

            def get_provider(self, model: str | None) -> str:
                return "openai"

            def _cli_args(self, *, model: str | None) -> list[str]:
                return []

        adapter = _MinimalAdapter(
            runner=FakeCommandRunner(),
            runtime_executor=_RecordingExecutor(),
        )
        assert adapter.hosted_env_passthrough_names == ()
        assert adapter.is_hosted is True
        # The hosted path uses the empty default: the request carries no names.
        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_base_default",
        )
        request = adapter._runtime_executor.calls[0]  # type: ignore[attr-defined]
        assert request.env_passthrough_names == ()
        # The profile_env field defaults to empty; an unreadable/absent compose
        # yields no literal profile values (fail-closed).
        assert request.profile_env == ()
