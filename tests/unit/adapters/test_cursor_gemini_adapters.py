"""Cursor adapter contract tests."""

from __future__ import annotations

import pytest

from awf.adapters.cursor import CursorAdapter
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
