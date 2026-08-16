"""Cursor and Gemini adapter contract tests."""

from __future__ import annotations

import pytest

from awf.adapters.cursor import CursorAdapter
from awf.adapters.gemini import GeminiAdapter
from awf.adapters.gemini import _settings_for_effort as gemini_settings_for_effort
from awf.adapters.gemini import _thinking_level_for_effort as gemini_thinking_level_for_effort
from awf.common.commands import FakeCommandRunner
from tests.unit.adapters.test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _assert_docker_exec_prefix,
    _assert_prompt_not_in_argv,
    _assert_prompt_sent_on_stdin,
)


class TestCursorAdapter:
    """Cursor adapter contract tests."""

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        """Verify produces correct cli invocation."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(runner=runner, default_model="composer")

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)

        cursor_start = args.index("cursor-agent")
        cursor_args = args[cursor_start:]
        assert cursor_args[:3] == ["cursor-agent", "-p", "--force"]
        assert cursor_args[cursor_args.index("--model") + 1] == "composer"
        assert "-m" not in cursor_args
        assert cursor_args[cursor_args.index("--output-format") + 1] == "text"
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_produces_cli_invocation_without_model_or_effort(self) -> None:
        """Verify produces cli invocation without model or effort."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        cursor_start = args.index("cursor-agent")
        cursor_args = args[cursor_start:]
        assert cursor_args[:3] == ["cursor-agent", "-p", "--force"]
        assert "--model" not in cursor_args
        assert cursor_args[cursor_args.index("--output-format") + 1] == "text"


class TestGeminiAdapter:
    """Gemini adapter contract tests."""

    @pytest.mark.unit
    def test_reports_google_provider(self) -> None:
        adapter = GeminiAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("gemini-3.1-pro-preview") == "google"

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        """Verify produces correct cli invocation."""
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
        """Verify produces cli invocation without model or effort."""
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
