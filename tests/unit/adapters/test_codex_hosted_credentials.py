"""Codex hosted credential contract tests.

Hosted execution should pass ``CODEX_API_KEY`` to ``codex exec`` (env
passthrough name). ``OPENAI_API_KEY`` may remain a *source* credential in
deployment systems, but ``codex exec`` itself must not require a workstation
``~/.codex`` directory in hosted mode. No secret values are ever transported
in the request, argv, stdin, or logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.adapters.registry  # noqa: F401 — populate registry
from awf.adapters.codex import CodexAdapter
from awf.adapters.runtime_executor import (
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
)
from awf.common.commands import FakeCommandRunner

_PROMPT = "Fix the typo in README."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")
_SECRET_VALUE = "sk-codex-secret-do-not-leak"


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        return AgentRuntimeExecResult(returncode=0, stdout="ok", stderr="")


class TestCodexHostedCredentials:
    @pytest.mark.unit
    async def test_hosted_path_surfaces_codex_api_key_name(self) -> None:
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
            workspace_id="ws_codex",
        )

        request = executor.calls[0]
        assert "CODEX_API_KEY" in request.env_passthrough_names

    @pytest.mark.unit
    async def test_codex_cli_argv_does_not_require_codex_dir(self) -> None:
        """``codex exec`` argv has no workstation ``~/.codex`` dependency."""
        executor = _RecordingExecutor()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            default_effort="high",
            runtime_executor=executor,
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            workspace_id="ws_codex_argv",
        )

        cli_args = list(executor.calls[0].cli_args)
        # codex exec reads the prompt from stdin ("-"), never a config path.
        assert cli_args[:3] == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        assert cli_args[-1] == "-"
        assert "--model" in cli_args and "gpt-5" in cli_args
        # No home-dir / config-dir assumptions in argv.
        assert not any(".codex" in arg for arg in cli_args)
        assert not any(arg.startswith("--config") for arg in cli_args)
        # OPENAI_API_KEY is NOT required by the codex CLI argv itself.
        assert "OPENAI_API_KEY" not in cli_args

    @pytest.mark.unit
    async def test_hosted_request_has_no_secret_values(self) -> None:
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
            workspace_id="ws_codex_nosec",
        )

        request = executor.calls[0]
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
        )
        assert _SECRET_VALUE not in blob
        assert "sk-" not in blob
        # Only the *name* is present, never a value.
        assert "CODEX_API_KEY=" not in blob
