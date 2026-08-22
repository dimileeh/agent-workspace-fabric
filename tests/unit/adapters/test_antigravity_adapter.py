"""Antigravity adapter contract tests — no real docker, no real CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest
import structlog

from awf.adapters.antigravity import AntigravityAdapter
from awf.adapters.base import AgentRunError
from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

from .test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _assert_docker_exec_prefix,
    _assert_prompt_not_in_argv,
    _assert_prompt_sent_on_stdin,
)


def _render_cli_script(*, model: str | None = "gemini-3.1-pro-preview") -> str:
    """Return the shell preamble from ``_cli_args`` without docker."""
    adapter = AntigravityAdapter(
        runner=FakeCommandRunner(),
        default_model=model,
        default_effort="xhigh",
    )
    args = adapter._cli_args(model=model)
    assert args[:2] == ["sh", "-lc"]
    return args[2]


async def _run_script_with_fake_agy(
    tmp_path: Path,
    *,
    agy_stdout: str,
    agy_returncode: int = 0,
) -> tuple[str, str, int | None]:
    """Run the rendered preamble against a stub agy emitting ``agy_stdout``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    payload = tmp_path / "agy_stdout.txt"
    payload.write_text(agy_stdout, encoding="utf-8")
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "sys.stdout.write(open(os.environ['AWF_FAKE_AGY_STDOUT'], encoding='utf-8').read())\n"
        "sys.stdout.flush()\n"
        f"sys.exit({agy_returncode})\n"
    )
    fake_agy.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(home),
            "AWF_FAKE_AGY_STDOUT": str(payload),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        _render_cli_script(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(b"prompt\n")
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


class TestAntigravityAdapter:
    """Antigravity adapter contract tests."""

    @pytest.mark.unit
    def test_reports_antigravity_provider(self) -> None:
        """Antigravity reports its own provider — never google/gemini."""
        adapter = AntigravityAdapter(runner=FakeCommandRunner())

        assert adapter.get_provider("gemini-3.1-pro-preview") == "antigravity"

    @pytest.mark.unit
    def test_hosted_env_passthrough_names_is_gemini_key_only(self) -> None:
        """Hosted passthrough carries GEMINI_API_KEY only; ANTIGRAVITY_API_KEY is retired."""
        adapter = AntigravityAdapter(runner=FakeCommandRunner())

        assert adapter.hosted_env_passthrough_names == ("GEMINI_API_KEY",)
        assert "ANTIGRAVITY_API_KEY" not in adapter.hosted_env_passthrough_names
        assert "GOOGLE_API_KEY" not in adapter.hosted_env_passthrough_names

    @pytest.mark.unit
    async def test_produces_correct_default_cli_invocation(self) -> None:
        """Default run uses $(cat) prompt bridge; -p is the final flag pair."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
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
        _assert_docker_exec_prefix(args)
        assert args[-3:-1] == ["sh", "-lc"]
        script = args[-1]
        assert "awf_prompt=$(cat)" in script
        assert "empty" in script.lower() or "prompt" in script.lower()
        assert "100000" in script
        assert "MAX_ARG_STRLEN" in script or "128" in script
        assert 'cat > "$prompt_path"' not in script
        assert "Read and execute the full task prompt" not in script
        # Options before -p; prompt last. Never -p immediately followed by a flag.
        assert re.search(r"(?m)-p\s+--", script) is None
        # agy runs under the stream-json decoder; the prompt stays the last pair.
        assert re.search(r'(?m)exec\s+python3\s+-c\s+.*-p\s+"\$awf_prompt"\s*$', script, re.S)
        assert '"agy"' in script
        assert "--dangerously-skip-permissions" in script
        assert "--output-format stream-json" in script
        assert "--print-timeout 24h" in script
        assert "--model gemini-3.1-pro-preview" in script
        assert "--effort" not in script
        assert "modelProvider" in script
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    def test_cli_script_guards_empty_and_oversize_prompt(self) -> None:
        """Preamble must fail closed on empty or oversize stdin prompts."""
        script = _render_cli_script()
        assert "awf_prompt=$(cat)" in script
        assert "100000" in script
        assert "MAX_ARG_STRLEN" in script or "128KiB" in script or "128" in script
        # Empty guard exits non-zero with a stderr message.
        assert "printf" in script
        assert "exit 1" in script or "exit 2" in script

    @pytest.mark.unit
    def test_settings_seed_keyed_on_gemini_api_key_only(self) -> None:
        """modelProvider=gemini seed requires GEMINI_API_KEY; not ANTIGRAVITY_API_KEY."""
        script = _render_cli_script()
        assert '[ -n "${GEMINI_API_KEY:-}" ]' in script
        assert "ANTIGRAVITY_API_KEY:-}${GEMINI_API_KEY" not in script
        assert "${ANTIGRAVITY_API_KEY:-}${GEMINI_API_KEY" not in script

    @pytest.mark.unit
    async def test_settings_updates_existing_non_gemini_provider(self, tmp_path: Path) -> None:
        """GEMINI_API_KEY mode rewrites modelProvider while preserving other settings."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_agy = bin_dir / "agy"
        fake_agy.write_text("#!/bin/sh\nexit 0\n")
        fake_agy.chmod(0o755)

        home = tmp_path / "home"
        settings = home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            '{"modelProvider":"vertexai","other":"preserve"}\n',
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "HOME": str(home),
                "GEMINI_API_KEY": "test-key",
            }
        )
        script = _render_cli_script()
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"ok\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        updated = json.loads(settings.read_text(encoding="utf-8"))
        assert updated["modelProvider"] == "gemini"
        assert updated["other"] == "preserve"

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
    async def test_model_override_is_passed_without_prompt_in_docker_argv(self) -> None:
        """Explicit models are passed; docker-exec argv still omits the prompt."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-preview",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            model="gemini-3.6-flash",
        )

        args = runner.calls[0].args
        script = args[-1]
        assert "--model gemini-3.6-flash" in script
        assert "--model gemini-3.1-pro-preview" not in script
        assert '-p "$awf_prompt"' in script
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_effort_is_accepted_but_unmapped_into_cli_args(self) -> None:
        """Effort is recorded on the adapter but never emitted; agy rejects --effort."""
        runner = FakeCommandRunner()
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-preview",
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
    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
        ],
    )
    def test_api_key_mode_allowlist_accepts_exact_slugs(self, model: str) -> None:
        """agy 1.1.13 API-key mode accepts exactly these three model slugs."""
        script = _render_cli_script(model=model)
        assert f"--model {model}" in script
        assert "--effort" not in script
        # Allowlisted models must not embed a GEMINI_API_KEY-gated reject.
        assert "API-key mode does not accept model" not in script

    @pytest.mark.unit
    def test_oauth_composite_model_slug_passes_through_cli_args(self) -> None:
        """OAuth composite slugs must not be rejected at Python argv-build time."""
        script = _render_cli_script(model="gemini-3.6-flash-high")
        assert "--model gemini-3.6-flash-high" in script
        # Reject is deferred to the GEMINI_API_KEY branch (API-key mode only).
        assert "API-key mode does not accept model" in script

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3.7-flash",
            "not-a-real-model",
            "gemini-3.6-flash-high",
        ],
    )
    async def test_api_key_mode_allowlist_rejects_outside_slugs_at_runtime(
        self,
        tmp_path: Path,
        model: str,
    ) -> None:
        """API-key mode (GEMINI_API_KEY set) rejects models outside the allowlist."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_agy = bin_dir / "agy"
        fake_agy.write_text("#!/bin/sh\nexit 0\n")
        fake_agy.chmod(0o755)

        script = _render_cli_script(model=model)
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "HOME": str(home),
                "GEMINI_API_KEY": "test-key-triggers-api-key-mode",
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()

        assert proc.returncode == 1
        message = stderr.decode()
        assert model in message
        assert "API-key mode does not accept model" in message
        for slug in (
            "gemini-3.1-pro-preview",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
        ):
            assert slug in message

    @pytest.mark.unit
    async def test_oauth_composite_slug_runs_without_gemini_api_key(
        self,
        tmp_path: Path,
    ) -> None:
        """Without GEMINI_API_KEY, OAuth composite slugs must reach agy."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_copy = tmp_path / "argv.json"
        fake_agy = bin_dir / "agy"
        fake_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['AWF_FAKE_AGY_ARGV'], 'w', encoding='utf-8') as fh:\n"
            "    json.dump(sys.argv[1:], fh)\n"
        )
        fake_agy.chmod(0o755)

        script = _render_cli_script(model="gemini-3.6-flash-high")
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env.pop("GEMINI_API_KEY", None)
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "HOME": str(home),
                "AWF_FAKE_AGY_ARGV": str(argv_copy),
            }
        )
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"oauth prompt\n")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        captured = json.loads(argv_copy.read_text())
        assert captured[captured.index("--model") + 1] == "gemini-3.6-flash-high"

    @pytest.mark.unit
    async def test_sh_stub_receives_prompt_as_final_p_value(self, tmp_path: Path) -> None:
        """Functional sh bridge: stub agy argv ends with -p <prompt>; flags intact."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_copy = tmp_path / "argv.json"
        fake_agy = bin_dir / "agy"
        fake_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['AWF_FAKE_AGY_ARGV'], 'w', encoding='utf-8') as fh:\n"
            "    json.dump(sys.argv[1:], fh)\n"
        )
        fake_agy.chmod(0o755)

        script = _render_cli_script(model="gemini-3.1-pro-preview")
        # Skip settings seeding side effects in HOME; use an empty HOME.
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "HOME": str(home),
                "AWF_FAKE_AGY_ARGV": str(argv_copy),
                "GEMINI_API_KEY": "test-key-not-used-by-stub",
            }
        )
        # Trailing newlines are stripped by $(cat) (POSIX command substitution);
        # that matches the verified online agy transport shape.
        prompt = "line one\nline two with 'quotes' and spaces"
        # Use sh -c (not -lc): login shells reset PATH and would hit the real agy.
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write((prompt + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()

        assert proc.returncode == 0, stderr.decode()
        captured = json.loads(argv_copy.read_text())
        assert captured[-2:] == ["-p", prompt]
        assert "--dangerously-skip-permissions" in captured
        assert captured[captured.index("--output-format") + 1] == "stream-json"
        assert captured[captured.index("--print-timeout") + 1] == "24h"
        assert captured[captured.index("--model") + 1] == "gemini-3.1-pro-preview"

    @pytest.mark.unit
    async def test_sh_stub_rejects_empty_prompt(self, tmp_path: Path) -> None:
        """Empty stdin must exit non-zero before exec'ing agy."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_agy = bin_dir / "agy"
        fake_agy.write_text("#!/bin/sh\necho should-not-run >&2\nexit 0\n")
        fake_agy.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env.update({"PATH": f"{bin_dir}:{env['PATH']}", "HOME": str(home)})
        script = _render_cli_script()
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()
        assert proc.returncode != 0
        assert b"should-not-run" not in stderr
        assert b"empty" in stderr.lower() or b"prompt" in stderr.lower()

    @pytest.mark.unit
    async def test_sh_stub_rejects_oversize_prompt(self, tmp_path: Path) -> None:
        """Oversize stdin must exit non-zero naming the MAX_ARG_STRLEN ceiling."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_agy = bin_dir / "agy"
        fake_agy.write_text("#!/bin/sh\necho should-not-run >&2\nexit 0\n")
        fake_agy.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        env = os.environ.copy()
        env.update({"PATH": f"{bin_dir}:{env['PATH']}", "HOME": str(home)})
        script = _render_cli_script()
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"x" * 100001)
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()
        _stdout, stderr = await proc.communicate()
        assert proc.returncode != 0
        assert b"should-not-run" not in stderr
        assert b"100000" in stderr
        assert b"MAX_ARG_STRLEN" in stderr or b"128" in stderr

    @pytest.mark.unit
    async def test_stream_json_events_decode_to_plaintext_verdict_lines(
        self,
        tmp_path: Path,
    ) -> None:
        """A repeated result event still yields one plaintext protocol record."""
        verdict_line = "AWF-VERDICT: NEEDS_HUMAN: need an operator decision on the retry policy"
        events = [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"Checked it.\n{verdict_line}"}]},
            },
            {"type": "result", "subtype": "success", "result": verdict_line},
        ]
        stdout, stderr, returncode = await _run_script_with_fake_agy(
            tmp_path,
            agy_stdout="".join(json.dumps(event) + "\n" for event in events),
        )

        assert returncode == 0, stderr
        assert f"\n{verdict_line}\n" in f"\n{stdout}"
        assert stdout.count(verdict_line) == 1
        assert '{"type"' not in stdout
        assert _parse_verdict_result(stdout) == VerdictResult(
            verdict="needs_human",
            reason="need an operator decision on the retry policy",
        )
        # Non-text events still stream (idle watchdog) — just not on stdout.
        assert '"session_id": "s-1"' in stderr

    @pytest.mark.unit
    async def test_stream_json_decoder_preserves_agy_exit_code(self, tmp_path: Path) -> None:
        """A failing agy run must not be masked by the decoder."""
        _stdout, stderr, returncode = await _run_script_with_fake_agy(
            tmp_path,
            agy_stdout=json.dumps({"type": "result", "subtype": "error_max_turns"}) + "\n",
            agy_returncode=7,
        )

        assert returncode == 7
        assert "error_max_turns" in stderr

    @pytest.mark.unit
    async def test_non_json_agy_output_passes_through_to_stdout(self, tmp_path: Path) -> None:
        """Plaintext agy output is already verdict-shaped; keep it on stdout."""
        stdout, stderr, returncode = await _run_script_with_fake_agy(
            tmp_path,
            agy_stdout="AWF-VERDICT: FIXED: decoded the stream\n",
        )

        assert returncode == 0, stderr
        assert _parse_verdict_result(stdout) == VerdictResult(
            verdict="fix_committed",
            reason="decoded the stream",
        )

    @pytest.mark.unit
    async def test_auth_failure_classifies_as_agent_auth_failed(self) -> None:
        """Real agy auth marker text classifies to generic AGENT_AUTH_FAILED."""
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=1,
            stderr="Error: authentication required. Run 'agy' to log in, then retry.",
        )
        adapter = AntigravityAdapter(
            runner=runner,
            default_model="gemini-3.1-pro-preview",
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
        assert exc.value.details["model"] == "gemini-3.1-pro-preview"
        provider_recovery = exc.value.details["provider_recovery"]
        assert provider_recovery["provider"] == "antigravity"
        assert any(
            event.get("event") == "agent.run.start"
            and event.get("model") == "gemini-3.1-pro-preview"
            for event in captured
        )
