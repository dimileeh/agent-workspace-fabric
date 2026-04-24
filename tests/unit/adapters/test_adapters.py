"""Adapter tests — no real docker, no real CLI.

Each test runs the adapter against a FakeCommandRunner that records the
argv it's handed and returns canned output. We verify:

1. The adapter produces the right ``docker compose exec`` invocation.
2. The CLI-specific flags match the reference pattern for each CLI.
3. Non-zero exit → AgentRunError with the agent name and full result.
4. The registry populates correctly on import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Importing the registry module forces adapter self-registration.
import awf.adapters.registry  # noqa: F401
from awf.adapters import get_adapter  # noqa: F401 - populates registry via __init__
from awf.adapters.base import AgentRunError
from awf.adapters.claude_code import ClaudeCodeAdapter
from awf.adapters.codex import CodexAdapter
from awf.adapters.gemini import GeminiAdapter
from awf.common.commands import FakeCommandRunner
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


class TestCodexAdapter:
    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = CodexAdapter(runner=runner, default_model="gpt-5")

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
        assert any(a.startswith("model_reasoning_effort=") for a in args)

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


class TestClaudeCodeAdapter:
    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = ClaudeCodeAdapter(runner=runner, default_model="sonnet")

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
        assert args[gemini_start : gemini_start + 2] == ["gemini", "--yolo"]
        assert args[-2] == "-p"
        # AWF prepends a contract preamble ("do not switch branches")
        # before the user-supplied prompt; the last argv element is
        # therefore the wrapped form. Check the user prompt is the
        # trailing substring so the assertion survives preamble edits.
        assert args[-1].endswith(_PROMPT)
        assert "AWF workspace contract" in args[-1]
        assert "-m" in args and "gemini-2.5-pro" in args


class TestRegistry:
    @pytest.mark.unit
    def test_all_three_adapters_registered(self) -> None:
        runner = FakeCommandRunner()

        codex = get_adapter(AgentRuntime.codex, runner=runner)
        claude = get_adapter(AgentRuntime.claude_code, runner=runner)
        gemini = get_adapter(AgentRuntime.gemini, runner=runner)

        assert codex.name == AgentRuntime.codex
        assert claude.name == AgentRuntime.claude_code
        assert gemini.name == AgentRuntime.gemini
