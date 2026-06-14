"""OpenCode adapter contract tests — no real docker, no real CLI.

Split out of ``test_adapters.py`` to keep each file under the maintainability
line limit. Shared helpers and fixtures are imported from ``test_adapters``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from awf.adapters.opencode import (
    OPENCODE_OLLAMA_CLOUD_MODELS,
    OpenCodeAdapter,
    _config_model_key,
    _ollama_base_url_prelude,
    _opencode_config_for_effort,
    _opencode_launcher_script,
    _qualified_model,
    _thinking_enabled,
    _variant_for_effort,
)
from awf.common.commands import FakeCommandRunner
from awf.service import provider_readiness_helpers

from .test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _assert_docker_exec_prefix,
    _assert_prompt_not_in_argv,
    _assert_prompt_sent_on_stdin,
)


class TestOpenCodeAdapter:
    """OpenCode adapter contract tests."""

    @pytest.mark.unit
    def test_reports_provider_from_selected_or_default_model(self) -> None:
        default_adapter = OpenCodeAdapter(runner=FakeCommandRunner())
        openai_adapter = OpenCodeAdapter(
            runner=FakeCommandRunner(),
            default_model="openai/gpt-oss",
        )

        assert default_adapter.get_provider(None) == "ollama"
        assert default_adapter.get_provider("ollama/glm-5.1:cloud") == "ollama"
        assert openai_adapter.get_provider(None) == "openai"
        assert openai_adapter.get_provider("anthropic/claude-sonnet") == "anthropic"

    @pytest.mark.unit
    @pytest.mark.parametrize("model", OPENCODE_OLLAMA_CLOUD_MODELS)
    async def test_runs_opencode_with_each_supported_ollama_cloud_model(
        self,
        model: str,
    ) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model=model,
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
        assert "OPENCODE_CONFIG_CONTENT" in script
        assert "AWF_OPENCODE_OLLAMA_BASE_URL" in script
        assert "host.docker.internal:11434/v1" in script
        assert "opencode run" in script
        assert "mktemp" in script
        assert "/tmp/awf-opencode-prompt.md" not in script
        assert '--file "$prompt_path"' in script
        assert '"permission":"allow"' in script
        assert '"think":true' in script
        assert model in script
        assert "--dangerously-skip-permissions" in args
        assert "--model" in args
        assert f"ollama/{model}" in args
        assert "--variant" in args
        assert "max" in args
        assert "--thinking" in args
        assert "--file" not in args
        assert args[-1] == "Follow the instructions in the attached AWF prompt file exactly."
        _assert_prompt_not_in_argv(args)
        _assert_prompt_sent_on_stdin(runner)

    @pytest.mark.unit
    async def test_preserves_fully_qualified_model_name(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model="ollama/glm-5.1:cloud",
            default_effort="max",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        assert "--model" in args
        assert "ollama/glm-5.1:cloud" in args

    @staticmethod
    async def _resolve_ollama_base_url(env_overrides: dict[str, str]) -> str:
        """Execute the launcher prelude under ``sh`` and return the resolved URL."""
        script = _ollama_base_url_prelude() + 'printf "%s" "$AWF_OPENCODE_OLLAMA_BASE_URL"\n'
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(env_overrides)
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        assert proc.returncode == 0, stderr.decode()
        return stdout.decode()

    @pytest.mark.unit
    async def test_launch_prelude_prefers_explicit_base_url(self) -> None:
        """An explicit ``AWF_OPENCODE_OLLAMA_BASE_URL`` wins over ``OLLAMA_HOST``."""
        resolved = await self._resolve_ollama_base_url(
            {
                "AWF_OPENCODE_OLLAMA_BASE_URL": "http://explicit.local:11434/v1",
                "OLLAMA_HOST": "http://ollama.local:11434",
            }
        )
        assert resolved == "http://explicit.local:11434/v1"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            # A port-less explicit base URL must inherit Ollama's default daemon
            # port (11434) so launch agrees with the worker probe/pull builder,
            # which defaults the same key to :11434 before probing.
            ("http://ollama-sidecar/v1", "http://ollama-sidecar:11434/v1"),
            ("http://ollama-sidecar", "http://ollama-sidecar:11434/v1"),
            ("ollama-sidecar", "http://ollama-sidecar:11434/v1"),
            ("https://ollama-sidecar/v1", "https://ollama-sidecar:11434/v1"),
            # An IPv6 loopback literal is host-local: it is translated to the Docker
            # host gateway (issue #579), keeping the defaulted daemon port.
            ("http://[::1]/v1", "http://host.docker.internal:11434/v1"),
            # A port-less value carrying userinfo credentials still inherits the
            # default daemon port (11434): the colon in ``user:pass`` is part of
            # the credentials, not a port, so it must not suppress defaulting.
            (
                "http://user:pass@ollama.local/v1",
                "http://user:pass@ollama.local:11434/v1",
            ),
            # A host-local host drops userinfo and normalizes to the host gateway:
            # the gateway needs no credentials and the agent must reach the host
            # daemon, not itself.
            (
                "http://user:pass@[::1]/v1",
                "http://host.docker.internal:11434/v1",
            ),
            # Userinfo with an explicit port is left intact.
            (
                "http://user:pass@ollama.local:9999/v1",
                "http://user:pass@ollama.local:9999/v1",
            ),
            # An explicit value that already carries a port is left intact.
            ("http://explicit.local:9999/v1", "http://explicit.local:9999/v1"),
        ],
    )
    async def test_launch_prelude_normalizes_explicit_base_url(
        self, base_url: str, expected: str
    ) -> None:
        """A port-less explicit base URL is normalized so the agent targets the
        same daemon AWF probes/pulls in the preflight."""
        resolved = await self._resolve_ollama_base_url({"AWF_OPENCODE_OLLAMA_BASE_URL": base_url})
        assert resolved == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("ollama_host", "expected"),
        [
            ("ollama.local:11434", "http://ollama.local:11434/v1"),
            ("http://ollama.local:11434", "http://ollama.local:11434/v1"),
            ("http://ollama.local:11434/", "http://ollama.local:11434/v1"),
            ("http://ollama.local:11434/v1", "http://ollama.local:11434/v1"),
            ("https://ollama.local:11434", "https://ollama.local:11434/v1"),
            # A port-less host must inherit Ollama's default daemon port
            # (11434) rather than collapsing to the scheme default (port 80).
            ("ollama-sidecar", "http://ollama-sidecar:11434/v1"),
            ("http://ollama-sidecar", "http://ollama-sidecar:11434/v1"),
            ("https://ollama-sidecar/v1", "https://ollama-sidecar:11434/v1"),
            ("ollama.local", "http://ollama.local:11434/v1"),
            # Host-local connection targets are translated to the Docker host gateway
            # (issue #579) so the agent reaches the host Ollama daemon, not itself.
            # The IPv4/IPv6 unspecified addresses, IPv4 loopback (any ``127.*``),
            # ``localhost``, and the IPv6 loopback all normalize to the gateway with
            # the resolved/defaulted daemon port kept.
            ("0.0.0.0", "http://host.docker.internal:11434/v1"),
            ("0.0.0.0:11434", "http://host.docker.internal:11434/v1"),
            ("127.0.0.1:11434", "http://host.docker.internal:11434/v1"),
            ("127.5.5.5:11434", "http://host.docker.internal:11434/v1"),
            ("localhost", "http://host.docker.internal:11434/v1"),
            ("http://localhost:11434", "http://host.docker.internal:11434/v1"),
            # ``localhost`` is host-local regardless of case: the Python preflight
            # lowercases the host, so the shell launcher must match every casing too
            # or the two disagree on the daemon.
            ("http://LocalHost:11434", "http://host.docker.internal:11434/v1"),
            ("LOCALHOST:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::1]", "http://host.docker.internal:11434/v1"),
            ("http://[::1]:11434", "http://host.docker.internal:11434/v1"),
            # An expanded/uncompressed IPv6 loopback or unspecified literal is
            # canonicalized to the host gateway like ``::1`` / ``::`` -- the Python
            # ``ipaddress`` check treats every textual form alike, so the shell must
            # too (issue #579).
            ("http://[0:0:0:0:0:0:0:1]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[0::1]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[0:0:0:0:0:0:0:0]:11434", "http://host.docker.internal:11434/v1"),
            # An IPv4-mapped IPv6 literal (``::ffff:<v4>``) inherits the embedded
            # IPv4's host-local status: Python's ``ipaddress`` reports a mapped
            # loopback/unspecified as such, but the dotted form is skipped by the
            # IPv6 canonicalization, so the prelude must reduce it to the embedded
            # IPv4 or launch keeps ``::ffff:127.0.0.1`` while preflight normalizes it
            # to the gateway (issue #579). The ``ffff`` prefix matches case-insensitively.
            ("http://[::ffff:127.0.0.1]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::ffff:127.5.5.5]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::ffff:0.0.0.0]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::FFFF:127.0.0.1]:11434", "http://host.docker.internal:11434/v1"),
            (
                "http://[0:0:0:0:0:ffff:127.0.0.1]:11434",
                "http://host.docker.internal:11434/v1",
            ),
            # An IPv6 loopback/unspecified literal carrying a zone/scope id
            # (``%<zone>``, or its percent-encoded ``%25`` form) is still host-local:
            # Python's ``ipaddress`` reports ``::1%lo`` as loopback (the zone does not
            # change loopback-ness), so the prelude must strip the zone before the
            # loopback match or launch keeps ``::1%lo`` while preflight normalizes it to
            # the gateway (issue #579).
            ("http://[::1%lo]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::1%25lo]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::%lo]:11434", "http://host.docker.internal:11434/v1"),
            ("http://[::ffff:127.0.0.1%lo]:11434", "http://host.docker.internal:11434/v1"),
            # A scoped IPv6 *non*-loopback (link-local) is not host-local: stripping the
            # zone leaves a routable literal that passes through unchanged on both sides.
            ("http://[fe80::1%lo]:11434", "http://[fe80::1%lo]:11434/v1"),
            # A userinfo-bearing host-local value drops the credentials too.
            ("http://user:pass@127.0.0.1:11434", "http://host.docker.internal:11434/v1"),
        ],
    )
    async def test_launch_prelude_mirrors_ollama_host(
        self, ollama_host: str, expected: str
    ) -> None:
        """``OLLAMA_HOST``-only profiles get a normalized base URL so the agent
        targets the same daemon AWF probes/pulls in the preflight."""
        resolved = await self._resolve_ollama_base_url({"OLLAMA_HOST": ollama_host})
        assert resolved == expected

    @pytest.mark.unit
    async def test_launch_prelude_falls_back_to_default(self) -> None:
        """With neither variable set the prelude keeps the default daemon URL."""
        resolved = await self._resolve_ollama_base_url({})
        assert resolved == "http://host.docker.internal:11434/v1"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "env_var",
        ["OLLAMA_HOST", "AWF_OPENCODE_OLLAMA_BASE_URL"],
    )
    @pytest.mark.parametrize(
        "ollama_host",
        [
            "127.0.0.1:11434",
            "http://localhost",
            "http://LocalHost:11434",
            "0.0.0.0:11434",
            "http://[::1]:11434",
            "[::]:11434",
            # Expanded/uncompressed IPv6 loopback and unspecified literals must
            # resolve the same daemon on both sides: the Python preflight uses
            # ``ipaddress.ip_address()`` (every textual form is loopback/unspecified),
            # so the shell prelude must canonicalize them too or launch keeps the
            # IPv6 loopback while the worker probes the gateway (issue #579).
            "http://[0:0:0:0:0:0:0:1]:11434",
            "http://[0000:0000:0000:0000:0000:0000:0000:0001]:11434",
            "http://[0::1]:11434",
            "http://[0:0:0:0:0:0:0:0]:11434",
            "http://[0::]:11434",
            # IPv4-mapped IPv6 literals carry the embedded IPv4's host-local status on
            # the Python side; the prelude must agree. A mapped loopback/unspecified
            # normalizes; a mapped routable IPv4, and an IPv4-*compatible* ``::127.0.0.1``
            # (which Python does not treat as loopback), pass through unchanged.
            "http://[::ffff:127.0.0.1]:11434",
            "http://[::ffff:0.0.0.0]:11434",
            "http://[::ffff:192.168.1.1]:11434",
            "http://[::127.0.0.1]:11434",
            "http://[64:ff9b::127.0.0.1]:11434",
            # IPv4-mapped IPv6 literals also have a *hex-compressed* spelling with no
            # embedded dotted quad (``::ffff:7f00:1`` == ``::ffff:127.0.0.1``).
            # ``ipaddress`` decodes the trailing two 16-bit groups as the mapped IPv4
            # and reports the same loopback/unspecified status, but the dotted-quad
            # reduction never matches the dot-less form and the IPv6 canonicalizer bails
            # on the non-zero ``ffff`` group -- so the prelude must reduce the hex form
            # too or launch keeps ``::ffff:7f00:1`` (the agent container itself) while
            # preflight normalizes to the gateway (issue #579 / comment 4492637683). A
            # mapped loopback (high group ``7f00..7fff``, any low group) and the mapped
            # unspecified (both groups zero) normalize; a mapped routable IPv4
            # (``c0a8:101`` == 192.168.1.1, ``8000:1`` == 128.0.0.1) and the
            # not-quite-loopback ``0:1`` (== 0.0.0.1) / ``7eff:ffff`` (== 126.255.255.255)
            # pass through unchanged.
            "http://[::ffff:7f00:1]:11434",
            "http://[::ffff:7f00:0]:11434",
            "http://[::ffff:7fff:ffff]:11434",
            "http://[::FFFF:7f00:1]:11434",
            "http://[::ffff:7f00:0001]:11434",
            "http://[0:0:0:0:0:ffff:7f00:1]:11434",
            "http://[0000:0000:0000:0000:0000:ffff:7f00:1]:11434",
            "http://[::ffff:7f00:1%lo]:11434",
            "http://[::ffff:0:0]:11434",
            "http://[::ffff:0000:0000]:11434",
            "http://[::ffff:0:1]:11434",
            "http://[::ffff:7eff:ffff]:11434",
            "http://[::ffff:8000:1]:11434",
            "http://[::ffff:c0a8:101]:11434",
            "http://[::ffff:007f:1]:11434",
            "http://[1::ffff:7f00:1]:11434",
            "http://[::ffff:ffff:7f00:1]:11434",
            "http://[::ffaf:7f00:1]:11434",
            # IPv6 zone/scope ids: ``ipaddress`` accepts the scoped form and keeps the
            # underlying loopback/link-local classification, so the prelude must strip
            # the zone and agree -- a scoped loopback normalizes, a scoped link-local
            # passes through (issue #579).
            "http://[::1%lo]:11434",
            "http://[::1%25lo]:11434",
            "http://[::%lo]:11434",
            "http://[::ffff:127.0.0.1%lo]:11434",
            "http://[fe80::1%lo]:11434",
            "host.docker.internal:11434",
            "ollama-sidecar:11434",
            "192.168.1.10:11434",
            "ollama.local",
            "http://user:pass@127.0.0.1:11434",
            # A DNS name that merely *starts* with ``127.`` is not a ``127.0.0.0/8``
            # literal: ``ipaddress.ip_address`` rejects it, so the Python side leaves it
            # pointed at its real host. The launcher must not let a bare ``127.*`` glob
            # rewrite it to the gateway, or launch and preflight diverge for these inputs.
            "127.0.0.1.nip.io:11434",
            "127.foo:11434",
            "127.0.0.1.2:11434",
            # Digit/dot-only ``127.*`` strings that are still not valid ``127.0.0.0/8``
            # literals: ``ipaddress.ip_address`` rejects an over-range octet, a
            # leading-zero octet, or an embedded empty octet, so the Python side leaves
            # them pointed at their real host. The launcher's octet validation must agree
            # and not rewrite them to the gateway (PRRT_kwDOSJAM6s6JYpBt).
            "127.0.0.256:11434",
            "127.00.0.1:11434",
            "127.0.0.01:11434",
            "127.0..1:11434",
            "127.0.0.:11434",
            # Boundary loopback literals that *are* valid and must normalize.
            "127.255.255.255:11434",
            "127.0.0.0:11434",
            # Malformed / whitespace-padded inputs: ``_parse_ollama_base_url`` strips
            # the value and forces ``urlsplit``'s lazy ``hostname`` / ``port`` accessors,
            # routing an unbalanced IPv6 bracket, a non-numeric / out-of-range port, and
            # a hostless value to ``DEFAULT_OLLAMA_OPENAI_BASE_URL``. The launcher prelude
            # must trim and fail closed the same way or it would forward a garbled base
            # URL while preflight probes the default daemon (comment 4492678514).
            " http://localhost:11434 ",
            "  127.0.0.1:11434  ",
            "\thttp://ollama-sidecar:11434\n",
            "http://",
            "://",
            "http://:11434",
            "http://[::1",
            "http://::1]:11434",
            "http://localhost:abc",
            "http://localhost:99999",
            "http://[::1]:notaport",
            "   ",
        ],
    )
    async def test_launch_prelude_matches_python_preflight_resolution(
        self, ollama_host: str, env_var: str
    ) -> None:
        """Parity anchor: the shell launcher prelude and the Python worker preflight
        must resolve the *same* daemon (host + port) for every representative input.

        Host-local targets normalize to ``host.docker.internal`` on both sides; routable
        hosts pass through unchanged. Running the actual prelude under ``sh`` against
        ``_parse_ollama_base_url`` is what keeps the two implementations honest — any
        sh-specific bracketed-IPv6/userinfo bug, or a drift between the host-local sets,
        surfaces as a host/port mismatch here. Both env keys are exercised because the
        prelude (and ``_parse_ollama_base_url``) prefer ``AWF_OPENCODE_OLLAMA_BASE_URL``
        over ``OLLAMA_HOST`` while sharing the downstream normalization, so a regression
        specific to the preferred branch must also surface as a mismatch."""
        env = {env_var: ollama_host}
        resolved = await self._resolve_ollama_base_url(env)
        shell_parts = urlsplit(resolved)
        python_parts = provider_readiness_helpers._parse_ollama_base_url(env)

        assert (shell_parts.hostname, shell_parts.port) == (
            python_parts.hostname,
            python_parts.port,
        )

    @pytest.mark.unit
    async def test_default_opencode_invocation_omits_variant_without_effort(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner)

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        assert "--model" in args
        assert "ollama/kimi-k2.6:cloud" in args
        assert "--variant" not in args
        assert "--thinking" not in args

    @pytest.mark.unit
    def test_opencode_effort_helpers_cover_default_and_high_paths(self) -> None:
        assert _qualified_model("kimi-k2.6:cloud") == "ollama/kimi-k2.6:cloud"
        assert _qualified_model("ollama/glm-5.1:cloud") == "ollama/glm-5.1:cloud"
        assert _thinking_enabled(None) is False
        assert _thinking_enabled("medium") is False
        assert _thinking_enabled("high") is True
        assert _variant_for_effort(None) is None
        assert _variant_for_effort("medium") is None
        assert _variant_for_effort("high") == "high"
        assert _variant_for_effort("xhigh") == "max"
        assert _variant_for_effort("max") == "max"

        low_config = _opencode_config_for_effort(effort=None)
        models = low_config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert all("options" not in model for model in models.values())

    @pytest.mark.unit
    def test_opencode_config_declares_arbitrary_non_allowlist_model(self) -> None:
        config = _opencode_config_for_effort(effort=None, model="ollama/kimi-k2.7:cloud")
        models = config["provider"]["ollama"]["models"]  # type: ignore[index]
        # The selected model is declared even though it is not in the default
        # fallback tuple — the old hardcoded-allowlist rejection is gone.
        assert "kimi-k2.7:cloud" not in OPENCODE_OLLAMA_CLOUD_MODELS
        assert "kimi-k2.7:cloud" in models
        assert models["kimi-k2.7:cloud"]["name"] == "kimi-k2.7:cloud"
        # The default fallback set remains available too.
        for default_model in OPENCODE_OLLAMA_CLOUD_MODELS:
            assert default_model in models

    @pytest.mark.unit
    def test_opencode_config_normalizes_bare_model_key(self) -> None:
        config = _opencode_config_for_effort(effort=None, model="llama4:70b")
        models = config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert "llama4:70b" in models
        assert "ollama/llama4:70b" not in models

    @pytest.mark.unit
    def test_opencode_config_omits_non_ollama_provider_model(self) -> None:
        # A provider-qualified model that belongs to another provider must not
        # leak into the ``ollama`` block — those entries would misroute runs.
        config = _opencode_config_for_effort(effort=None, model="openai/gpt-x")
        models = config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert "openai/gpt-x" not in models
        # The default Ollama fallback set is preserved untouched.
        assert set(models) == set(OPENCODE_OLLAMA_CLOUD_MODELS)

    @pytest.mark.unit
    def test_opencode_config_declares_slash_bearing_ollama_model(self) -> None:
        # A daemon-served model such as ``ollama/hf.co/...`` normalizes to a key
        # that still contains a ``/``; it must still be declared in the
        # ``ollama`` block so OpenCode does not reject the selected model.
        config = _opencode_config_for_effort(effort=None, model="ollama/hf.co/unsloth/model:Q4_K_M")
        models = config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert "hf.co/unsloth/model:Q4_K_M" in models
        assert models["hf.co/unsloth/model:Q4_K_M"]["name"] == "hf.co/unsloth/model:Q4_K_M"

    @pytest.mark.unit
    def test_opencode_config_default_fallback_when_no_model(self) -> None:
        config = _opencode_config_for_effort(effort=None, model=None)
        models = config["provider"]["ollama"]["models"]  # type: ignore[index]
        assert set(models) == set(OPENCODE_OLLAMA_CLOUD_MODELS)

    @pytest.mark.unit
    def test_config_model_key_strips_ollama_prefix_only(self) -> None:
        assert _config_model_key("ollama/kimi-k2.7:cloud") == "kimi-k2.7:cloud"
        assert _config_model_key("llama4:70b") == "llama4:70b"
        assert _config_model_key("foo:bar") == "foo:bar"
        # Only the ``ollama/`` provider prefix is stripped.
        assert _config_model_key("openai/gpt-x") == "openai/gpt-x"

    @pytest.mark.unit
    async def test_cli_model_and_embedded_config_agree(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model="ollama/foo:bar",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        assert "--model" in args
        assert "ollama/foo:bar" in args
        sh_start = [i for i, arg in enumerate(args) if arg == "sh"][-1]
        script = args[sh_start + 2]
        # The embedded config JSON declares the selected model under its bare
        # key, so the launched ``--model`` and the config never disagree.
        assert '"foo:bar"' in script

    @pytest.mark.unit
    async def test_cli_model_normalizes_surrounding_whitespace(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(
            runner=runner,
            default_model="  ollama/foo:bar  ",
            default_effort="xhigh",
        )

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        model_index = args.index("--model")
        # The ``--model`` flag is normalized once, so it never carries stray
        # whitespace that the stripped config key would disagree with.
        assert args[model_index + 1] == "ollama/foo:bar"
        sh_start = [i for i, arg in enumerate(args) if arg == "sh"][-1]
        script = args[sh_start + 2]
        assert '"foo:bar"' in script

    @pytest.mark.unit
    async def test_cli_model_falls_back_when_only_whitespace(self) -> None:
        runner = FakeCommandRunner()
        adapter = OpenCodeAdapter(runner=runner, default_model="   ")

        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
        )

        args = runner.calls[0].args
        model_index = args.index("--model")
        assert args[model_index + 1] == f"ollama/{OPENCODE_OLLAMA_CLOUD_MODELS[0]}"

    @pytest.mark.unit
    async def test_opencode_launcher_forwards_termination_and_cleans_temp_files(
        self,
        tmp_path: Path,
    ) -> None:
        bin_dir = tmp_path / "bin"
        tmp_dir = tmp_path / "tmp"
        bin_dir.mkdir()
        tmp_dir.mkdir()
        fake_opencode = bin_dir / "opencode"
        fake_started = tmp_path / "started"
        fake_signal = tmp_path / "signal"
        fake_prompt = tmp_path / "prompt-copy"
        fake_opencode.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "prompt_path=\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = "--file" ]; then\n'
            "    shift\n"
            '    prompt_path="$1"\n'
            "  fi\n"
            "  shift || true\n"
            "done\n"
            'cat "$prompt_path" > "$AWF_FAKE_PROMPT_COPY"\n'
            "trap 'printf TERM > \"$AWF_FAKE_SIGNAL\"; exit 143' TERM\n"
            'printf started > "$AWF_FAKE_STARTED"\n'
            "while :; do sleep 1; done\n"
        )
        fake_opencode.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{bin_dir}:{env['PATH']}",
                "TMPDIR": str(tmp_dir),
                "AWF_FAKE_STARTED": str(fake_started),
                "AWF_FAKE_SIGNAL": str(fake_signal),
                "AWF_FAKE_PROMPT_COPY": str(fake_prompt),
            }
        )

        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            _opencode_launcher_script(effort="xhigh"),
            "awf-opencode",
            "--model",
            "ollama/kimi-k2.6:cloud",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(b"workspace prompt")
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        for _ in range(250):
            if fake_started.exists():
                break
            await asyncio.sleep(0.02)
        assert fake_started.exists()

        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)

        assert proc.returncode == 143
        assert fake_signal.read_text() == "TERM"
        assert fake_prompt.read_text() == "workspace prompt"
        assert list(tmp_dir.glob("awf-opencode-prompt.*.md")) == []
        assert list(tmp_dir.glob("awf-opencode-config.*.json")) == []
