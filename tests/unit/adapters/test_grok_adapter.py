"""Grok Build adapter contract tests — no real docker, no real CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from awf.adapters.grok import GrokAdapter, _grok_launcher_script, _model_for_effort
from awf.common.commands import FakeCommandRunner

from .test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _assert_docker_exec_prefix,
    _assert_prompt_not_in_argv,
    _assert_prompt_sent_on_stdin,
)


class TestGrokAdapter:
    """Grok Build adapter contract tests."""

    @pytest.mark.unit
    def test_reports_xai_provider(self) -> None:
        adapter = GrokAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("grok-build") == "xai"

    @pytest.mark.unit
    async def test_produces_correct_cli_invocation(self) -> None:
        runner = FakeCommandRunner()
        adapter = GrokAdapter(
            runner=runner,
            default_model="grok-build",
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
        assert 'prompt="$(cat; printf x)"' in script
        assert 'prompt="${prompt%x}"' in script
        assert 'exec grok -p "$prompt" "$@"' in script
        grok_args = args[sh_start + 4 :]
        assert grok_args == [
            "--always-approve",
            "--no-alt-screen",
            "--no-auto-update",
            "--output-format",
            "plain",
            "-m",
            "grok-build",
        ]
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_produces_cli_invocation_without_model_or_effort(self) -> None:
        runner = FakeCommandRunner()
        adapter = GrokAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        sh_start = [i for i, arg in enumerate(args) if arg == "sh"][-1]
        assert args[sh_start + 4 :] == [
            "--always-approve",
            "--no-alt-screen",
            "--no-auto-update",
            "--output-format",
            "plain",
        ]
        assert "-m" not in args
        assert "--model" not in args

    @pytest.mark.unit
    def test_effort_mapping_keeps_selected_model_without_undocumented_flags(self) -> None:
        for effort in (None, "low", "medium", "high", "xhigh", "max"):
            assert _model_for_effort(model="grok-build", effort=effort) == "grok-build"
        assert _model_for_effort(model=None, effort="xhigh") is None

    @pytest.mark.unit
    async def test_launcher_reads_stdin_and_passes_prompt_to_official_single_flag(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        argv_copy = tmp_path / "argv.json"
        fake_grok.write_text(
            "#!/bin/sh\n"
            '"$AWF_TEST_PYTHON" - <<\'PY\' "$@"\n'
            "import json, os, sys\n"
            "with open(os.environ['AWF_FAKE_GROK_ARGV'], 'w', encoding='utf-8') as fh:\n"
            "    json.dump(sys.argv[1:], fh)\n"
            "PY\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AWF_FAKE_GROK_ARGV": str(argv_copy),
                "AWF_TEST_PYTHON": sys.executable,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-alt-screen",
            "--no-auto-update",
            "--output-format",
            "plain",
            "-m",
            "grok-build",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt\n\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        assert stdout == b""
        assert json.loads(argv_copy.read_text()) == [
            "-p",
            "workspace prompt\n\n",
            "--always-approve",
            "--no-alt-screen",
            "--no-auto-update",
            "--output-format",
            "plain",
            "-m",
            "grok-build",
        ]
