"""Antigravity adapter contract tests — no real docker, no real CLI."""

from __future__ import annotations

import pytest
import structlog

from awf.adapters.antigravity import AntigravityAdapter
from awf.adapters.base import AgentRunError
from awf.common.commands import FakeCommandRunner

from .test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _assert_docker_exec_prefix,
    _assert_prompt_not_in_argv,
    _assert_prompt_sent_on_stdin,
)


class TestAntigravityAdapter:
    """Antigravity adapter contract tests."""

    @pytest.mark.unit
    def test_reports_antigravity_provider(self) -> None:
        """Antigravity reports its own provider — never google/gemini."""
        adapter = AntigravityAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("gemini-3.1-pro-high") == "antigravity"

    @pytest.mark.unit
    def test_hosted_env_passthrough_names_are_antigravity_and_gemini_keys(self) -> None:
        """Hosted passthrough carries both env keys; no GOOGLE_API_KEY alias."""
        adapter = AntigravityAdapter(runner=FakeCommandRunner())

        assert adapter.hosted_env_passthrough_names == (
            "ANTIGRAVITY_API_KEY",
            "GEMINI_API_KEY",
        )
        assert "GOOGLE_API_KEY" not in adapter.hosted_env_passthrough_names

    @pytest.mark.unit
    async def test_produces_correct_default_cli_invocation(self) -> None:
        """Default run uses shell preamble, skip-permissions, stream-json, model."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-high",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        _assert_docker_exec_prefix(args)
        assert args[-3:-1] == ["sh", "-lc"]
        script = args[-1]
        assert 'cat > "$prompt_path"' in script
        assert "exec agy -p" in script
        assert "--dangerously-skip-permissions" in script
        assert "--output-format stream-json" in script
        assert "--print-timeout 24h" in script
        assert "--model gemini-3.1-pro-high" in script
        assert "--effort" not in script
        assert "modelProvider" in script
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_no_model_omits_model_flag(self) -> None:
        """Without a model default/override, --model is omitted."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        script = runner.calls[0].args[-1]
        assert "--model" not in script
        assert "--dangerously-skip-permissions" in script

    @pytest.mark.unit
    async def test_model_override_is_passed_without_prompt_argv(self) -> None:
        """Explicit models are passed while prompts remain stdin-only."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-high",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            model="gemini-3.7-flash-high",
        )

        args = runner.calls[0].args
        script = args[-1]
        assert "--model gemini-3.7-flash-high" in script
        assert "--model gemini-3.1-pro-high" not in script
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_effort_is_accepted_but_unmapped_into_cli_args(self) -> None:
        """Effort is recorded on the adapter but never mapped to --effort in v1."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-high",
            default_effort="high",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        assert adapter._default_effort == "high"
        script = runner.calls[0].args[-1]
        assert "--effort" not in script

    @pytest.mark.unit
    async def test_auth_failure_classifies_as_agent_auth_failed(self) -> None:
        """Auth marker text classifies to generic AGENT_AUTH_FAILED (not ANTIGRAVITY_*)."""
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stderr="authentication required: antigravity_api_key rejected",
        )
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-high",
            default_effort="xhigh",
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
        assert exc.value.details["provider"] == "antigravity"
        assert exc.value.details["model"] == "gemini-3.1-pro-high"
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["provider"] == "antigravity"
        assert any(
            event.get("event") == "agent.run.start" and event.get("model") == "gemini-3.1-pro-high"
            for event in captured
        )
