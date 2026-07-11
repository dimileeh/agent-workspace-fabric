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
        assert "grok agent stdio" in script
        assert "-p" not in script
        assert "$prompt" not in script
        grok_args = args[sh_start + 4 :]
        assert grok_args == [
            "--always-approve",
            "--no-auto-update",
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
            "--no-auto-update",
        ]
        assert "-m" not in args
        assert "--model" not in args

    @pytest.mark.unit
    def test_effort_mapping_keeps_selected_model_without_undocumented_flags(self) -> None:
        for effort in (None, "low", "medium", "high", "xhigh", "max"):
            assert _model_for_effort(model="grok-build", effort=effort) == "grok-build"
        assert _model_for_effort(model=None, effort="xhigh") is None

    @pytest.mark.unit
    async def test_launcher_reads_stdin_and_forwards_prompt_over_acp_stdio(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        argv_copy = tmp_path / "argv.json"
        prompt_copy = tmp_path / "prompt.json"
        fake_grok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['AWF_FAKE_GROK_ARGV'], 'w', encoding='utf-8') as fh:\n"
            "    json.dump(sys.argv[1:], fh)\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request['method']\n"
            "    if method == 'initialize':\n"
            "        result = {'authMethods': [{'id': 'cached_token'}]}\n"
            "    elif method == 'authenticate':\n"
            "        result = {}\n"
            "    elif method == 'session/new':\n"
            "        result = {'sessionId': 'session-1'}\n"
            "    elif method == 'session/prompt':\n"
            "        prompt = request['params']['prompt'][0]['text']\n"
            "        with open(os.environ['AWF_FAKE_GROK_PROMPT'], 'w', encoding='utf-8') as fh:\n"
            "            json.dump(prompt, fh)\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'text': 'done'}}}}), flush=True)\n"
            "        result = {'stopReason': 'end_turn'}\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AWF_FAKE_GROK_ARGV": str(argv_copy),
                "AWF_FAKE_GROK_PROMPT": str(prompt_copy),
                "AWF_TEST_PYTHON": sys.executable,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-auto-update",
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
        assert stdout == b"done\n"
        assert json.loads(argv_copy.read_text()) == [
            "--always-approve",
            "--no-auto-update",
            "-m",
            "grok-build",
            "agent",
            "stdio",
        ]
        assert json.loads(prompt_copy.read_text()) == "workspace prompt\n\n"

    @pytest.mark.unit
    async def test_launcher_drains_buffered_updates_after_prompt_response(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        fake_grok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request['method']\n"
            "    if method == 'initialize':\n"
            "        result = {'authMethods': [{'id': 'cached_token'}]}\n"
            "    elif method == 'authenticate':\n"
            "        result = {}\n"
            "    elif method == 'session/new':\n"
            "        result = {'sessionId': 'session-1'}\n"
            "    elif method == 'session/prompt':\n"
            "        response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'stopReason': 'end_turn'}}\n"
            "        update = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'text': 'after-response'}}}}\n"
            "        os.write(sys.stdout.fileno(), (json.dumps(response) + '\\n' + json.dumps(update) + '\\n').encode('utf-8'))\n"
            "        continue\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-auto-update",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        assert stdout == b"after-response\n"

    @pytest.mark.unit
    async def test_launcher_drains_delayed_updates_after_prompt_response(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        fake_grok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request['method']\n"
            "    if method == 'initialize':\n"
            "        result = {'authMethods': [{'id': 'cached_token'}]}\n"
            "    elif method == 'authenticate':\n"
            "        result = {}\n"
            "    elif method == 'session/new':\n"
            "        result = {'sessionId': 'session-1'}\n"
            "    elif method == 'session/prompt':\n"
            "        response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {'stopReason': 'end_turn'}}\n"
            "        print(json.dumps(response), flush=True)\n"
            "        time.sleep(0.45)\n"
            "        update = {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'text': 'delayed'}}}}\n"
            "        print(json.dumps(update), flush=True)\n"
            "        continue\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-auto-update",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        assert stdout == b"delayed\n"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("auth_methods", "expected_method"),
        [
            ([{"id": "cached_token"}], "cached_token"),
            ([{"id": "cached_token"}, {"id": "xai.api_key"}], "xai.api_key"),
        ],
    )
    async def test_launcher_respects_advertised_auth_methods_with_xai_api_key(
        self,
        tmp_path: Path,
        auth_methods: list[dict[str, str]],
        expected_method: str,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        auth_method_copy = tmp_path / "auth_method.json"
        fake_grok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request['method']\n"
            "    if method == 'initialize':\n"
            "        result = {'authMethods': json.loads(os.environ['AWF_FAKE_GROK_AUTH_METHODS'])}\n"
            "    elif method == 'authenticate':\n"
            "        with open(os.environ['AWF_FAKE_GROK_AUTH_METHOD'], 'w', encoding='utf-8') as fh:\n"
            "            json.dump(request['params']['methodId'], fh)\n"
            "        result = {}\n"
            "    elif method == 'session/new':\n"
            "        result = {'sessionId': 'session-1'}\n"
            "    elif method == 'session/prompt':\n"
            "        result = {'stopReason': 'end_turn'}\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "AWF_FAKE_GROK_AUTH_METHODS": json.dumps(auth_methods),
                "AWF_FAKE_GROK_AUTH_METHOD": str(auth_method_copy),
                "XAI_API_KEY": "xai-test-key",
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-auto-update",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        _, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        assert json.loads(auth_method_copy.read_text()) == expected_method

    @pytest.mark.unit
    async def test_launcher_returns_child_exit_code_after_successful_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_grok = bin_dir / "grok"
        fake_grok.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request['method']\n"
            "    if method == 'initialize':\n"
            "        result = {'authMethods': [{'id': 'cached_token'}]}\n"
            "    elif method == 'session/new':\n"
            "        result = {'sessionId': 'session-1'}\n"
            "    elif method == 'session/prompt':\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'text': 'partial'}}}}), flush=True)\n"
            "        result = {'stopReason': 'end_turn'}\n"
            "    else:\n"
            "        result = {}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
            "sys.exit(23)\n"
        )
        fake_grok.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _grok_launcher_script(),
            "awf-grok",
            "--always-approve",
            "--no-auto-update",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        stdout, stderr = await proc.communicate()

        assert proc.returncode == 23, stderr.decode()
        assert stdout == b"partial\n"
