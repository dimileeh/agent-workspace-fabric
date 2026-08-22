"""Cursor adapter contract tests — no real docker, no real CLI."""

from __future__ import annotations

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.adapters.cursor import CursorAdapter
from awf.adapters.model_selection import cursor_model_for_effort, cursor_selected_model
from awf.common.commands import FakeCommandRunner

from .test_adapters import (
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
            default_model="auto",
            default_effort=None,
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
            "--model",
            "auto",
            "--output-format",
            "text",
        ]
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_generic_effort_does_not_replace_auto_default(self) -> None:
        """Cursor Auto routing profiles are independent of AWF reasoning effort."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(
            runner=runner,
            default_model="auto",
            default_effort="medium",
        )

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
            "--model",
            "auto",
            "--output-format",
            "text",
        ]
        assert "-m" not in args[cursor_start:]

    @pytest.mark.unit
    async def test_generic_effort_failure_metadata_reports_auto_model(self) -> None:
        """Cursor failure metadata follows the Auto model actually selected for CLI."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="cursor-agent failed: cursor auth required")
        adapter = CursorAdapter(
            runner=runner,
            default_model="auto",
            default_effort="medium",
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(AgentRunError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
            )

        assert exc.value.reason_code == "AGENT_AUTH_FAILED"
        assert exc.value.details["provider"] == "cursor"
        assert exc.value.details["model"] == "auto"
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["provider"] == "cursor"
        assert provider_recovery["model"] == "auto"
        assert any(
            event.get("event") == "agent.run.start" and event.get("model") == "auto"
            for event in captured
        )

    @pytest.mark.unit
    async def test_explicit_lower_effort_failure_metadata_reports_selected_model(self) -> None:
        """Cursor failure metadata keeps an explicit lower-effort model override."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="cursor-agent failed: cursor auth required")
        adapter = CursorAdapter(
            runner=runner,
            default_model="sonnet-4-thinking",
            default_effort="medium",
        )

        with pytest.raises(AgentRunError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                model="sonnet-4-thinking",
            )

        assert exc.value.reason_code == "AGENT_AUTH_FAILED"
        assert exc.value.details["provider"] == "cursor"
        assert exc.value.details["model"] == "sonnet-4-thinking"
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["provider"] == "cursor"
        assert provider_recovery["model"] == "sonnet-4-thinking"

    @pytest.mark.unit
    async def test_explicit_thinking_model_override_is_preserved_for_lower_effort(self) -> None:
        """Explicit Cursor model overrides win even when effort is lower."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(
            runner=runner,
            default_model="sonnet-4-thinking",
            default_effort="medium",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            model="sonnet-4-thinking",
        )

        args = runner.calls[0].args
        cursor_start = args.index("cursor-agent")
        assert args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "--model",
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
            "--model",
            "gpt-5",
            "--output-format",
            "text",
        ]
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_parameterized_router_profile_override_is_passed_unchanged(self) -> None:
        """Eligible Cursor teams can select an official Auto Router profile."""
        runner = FakeCommandRunner()
        adapter = CursorAdapter(runner=runner, default_model="auto")
        router_model = "auto-smart[optimize_for=intelligence]"

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            model=router_model,
        )

        args = runner.calls[0].args
        cursor_start = args.index("cursor-agent")
        assert args[cursor_start:] == [
            "cursor-agent",
            "-p",
            "--force",
            "--model",
            router_model,
            "--output-format",
            "text",
        ]

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
    def test_generic_effort_never_selects_a_cursor_model_or_router_profile(self) -> None:
        """Cursor Auto profiles are not generic AWF reasoning-effort levels."""
        assert cursor_model_for_effort(model="gpt-5", effort="xhigh") == "gpt-5"
        assert cursor_model_for_effort(model="sonnet-4", effort="high") == "sonnet-4"
        assert cursor_model_for_effort(model=None, effort=None) is None
        assert cursor_model_for_effort(model=None, effort="medium") is None
        assert cursor_model_for_effort(model=None, effort="high") is None
        assert cursor_model_for_effort(model=None, effort="xhigh") is None
        assert cursor_model_for_effort(model=None, effort="max") is None

    @pytest.mark.unit
    def test_custom_default_model_bypasses_effort_mapping(self) -> None:
        """Custom Cursor defaults remain operator-controlled for high efforts."""
        assert (
            cursor_selected_model(
                model=None,
                default_model="gpt-5",
                effort="xhigh",
            )
            == "gpt-5"
        )
