"""Unit coverage for provider setup orchestration (T07).

These tests inject fakes for the GitHub ``gh`` subprocess probe, the Ollama HTTP
probe, and the credential keyring backend, so no test touches a real keychain,
network, or ``gh``. The fixed fake tokens let every case assert a raw secret
never escapes into a ref, summary, result, or persisted config.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.host_setup.config import HostSetupConfig, ProviderConfig
from awf.host_setup.credentials import (
    CREDENTIAL_REF_INVALID,
    CredentialError,
    CredentialRef,
    KeyringCredentialBackend,
)
from awf.host_setup.providers import (
    PROVIDER_REGISTRY,
    ProviderSetupSummary,
    orchestrate_provider_setup,
    render_provider_summary,
)
from awf.host_setup.rendering import INTERACTIVE_INPUT_REQUIRED, PROVIDER_SETUP_AUTH_INVALID
from awf.host_setup.system_checks import KNOWN_SETUP_PROVIDERS
from awf.service.config import ServiceSettings
from tests.unit.service.test_host_setup_credentials_parts._helpers import (
    _FAKE_GH_TOKEN,
    _FAKE_TOKEN,
    FakeKeyringModule,
    _FailBackend,
)


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


def _keyring_backend() -> KeyringCredentialBackend:
    return KeyringCredentialBackend(keyring_module=FakeKeyringModule())


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _SubprocessSpy:
    """Records each subprocess probe and returns a configured ``gh`` result."""

    def __init__(self, *, returncode: int = 0, raise_exc: BaseException | None = None) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._returncode = returncode
        self._raise = raise_exc

    def __call__(self, args: list[str], *, timeout: float, **_kwargs: object) -> Any:
        self.calls.append(args)
        self.timeouts.append(timeout)
        if self._raise is not None:
            raise self._raise
        return _completed(returncode=self._returncode)


class _HttpSpy:
    """Records each HTTP probe; returns a healthy or failing Ollama response."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.calls: list[str] = []
        self.timeouts: list[float] = []
        self._healthy = healthy

    def __call__(self, url: str, *, timeout: float) -> Any:
        self.calls.append(url)
        self.timeouts.append(timeout)
        if not self._healthy:
            return SimpleNamespace(status_code=503, text="unavailable")
        if url.endswith("/api/version"):
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        return SimpleNamespace(status_code=200, text='{"models":[]}')


def _unexpected_subprocess(args: list[str], **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected subprocess call: {args}")


def _unexpected_http(url: str, **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected HTTP call: {url}")


# --- Registry / name-surface bridge --------------------------------------


@pytest.mark.unit
def test_registry_covers_every_known_setup_provider() -> None:
    """Every setup provider maps to a readiness provider or a declared stub slot."""
    registry_names = {spec.name for spec in PROVIDER_REGISTRY}
    assert registry_names == set(KNOWN_SETUP_PROVIDERS)
    for spec in PROVIDER_REGISTRY:
        if spec.stub:
            assert spec.readiness_provider is None
            assert spec.stub_reason_code and spec.stub_summary
        else:
            assert spec.readiness_provider is not None


# --- Provider success / missing / invalid / mixed ------------------------


@pytest.mark.unit
def test_provider_success_stores_ref_and_rechecks_ready(tmp_path: Path) -> None:
    """A captured secret is stored as a safe ref and the recheck reports ready."""
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        capture=lambda provider: _FAKE_TOKEN if provider == "codex" else None,
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    result = summary.result_for("codex")
    assert result is not None
    assert result.status == "ready"
    assert result.configured is True
    assert result.rechecked is True
    assert result.credential_ref is not None
    assert result.credential_ref.startswith("keyring://")
    assert result.backend == "keyring"
    stored = config.providers["codex"]
    assert stored.credential_ref == result.credential_ref
    assert stored.status == "ready"


@pytest.mark.unit
def test_provider_missing_credential_is_not_configured_and_unchanged(tmp_path: Path) -> None:
    """No secret/env leaves the provider not_configured and its config untouched."""
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=False,
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    result = summary.result_for("codex")
    assert result is not None
    assert result.status == "not_configured"
    assert result.configured is False
    assert "codex" not in config.providers


@pytest.mark.unit
def test_recheck_without_fresh_secret_preserves_existing_config(tmp_path: Path) -> None:
    """A recheck with no fresh secret/env preserves a prior config, not not_configured."""
    existing = ProviderConfig(
        credential_ref="keyring://awf/codex",
        backend="keyring",
        status="ready",
        source="captured",
    )
    base = HostSetupConfig(providers={"codex": existing})

    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=base,
        allow_plain_secrets=False,
        non_interactive=False,
        environ={},
        capture=lambda _provider: None,
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    result = summary.result_for("codex")
    assert result is not None
    # The summary reflects the persisted (last-known) state instead of falsely
    # reporting not_configured, and flags that it was not actually re-probed.
    assert result.status == "ready"
    assert result.configured is True
    assert result.rechecked is False
    assert result.reason_code == "CODEX_PRESERVED"
    assert result.backend == "keyring"
    assert result.credential_ref == "keyring://awf/codex"
    # The persisted config entry stays exactly as it was.
    assert config.providers["codex"] == existing


@pytest.mark.unit
def test_recheck_preserves_existing_unavailable_config(tmp_path: Path) -> None:
    """Preserving a non-ready entry reports its last-known status, never ready."""
    existing = ProviderConfig(
        credential_ref="env://OPENAI_API_KEY",
        backend="env_ref",
        status="unavailable",
        source="env",
    )
    base = HostSetupConfig(providers={"codex": existing})

    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=base,
        allow_plain_secrets=False,
        non_interactive=False,
        environ={},
        capture=lambda _provider: None,
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    result = summary.result_for("codex")
    assert result is not None
    assert result.status == "unavailable"
    assert result.configured is True
    assert result.rechecked is False
    assert result.reason_code == "CODEX_PRESERVED"
    assert config.providers["codex"] == existing


@pytest.mark.unit
def test_provider_invalid_credential_marks_unavailable(tmp_path: Path) -> None:
    """A configured GitHub token rejected by ``gh`` is reported unavailable."""
    spy = _SubprocessSpy(returncode=1)
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        run_subprocess=spy,
        http_get=_unexpected_http,
    )

    result = summary.result_for("github")
    assert result is not None
    assert result.status == "unavailable"
    assert result.reason_code == PROVIDER_SETUP_AUTH_INVALID
    assert _FAKE_GH_TOKEN not in json.dumps(result.model_dump())
    # The persisted config records the unavailable state (never a ready ref) so it
    # cannot silently disagree with the summary; the raw token never leaks into it.
    assert config.providers["github"].status == "unavailable"
    assert _FAKE_GH_TOKEN not in json.dumps(config.providers["github"].model_dump())


@pytest.mark.unit
def test_mixed_provider_partial_readiness_is_independent(tmp_path: Path) -> None:
    """One ready, one invalid, one missing resolve independently and never block."""
    gh_ok = _SubprocessSpy(returncode=0)
    ollama_down = _HttpSpy(healthy=False)
    summary, _config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github", "opencode", "claude_code"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=False,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN, "OLLAMA_API_KEY": "ollama-xyz"},
        run_subprocess=gh_ok,
        http_get=ollama_down,
    )

    assert summary.overall_status == "partial"
    github = summary.result_for("github")
    opencode = summary.result_for("opencode")
    claude = summary.result_for("claude_code")
    assert github is not None and github.status == "ready"
    assert opencode is not None and opencode.status == "unavailable"
    assert claude is not None and claude.status == "not_configured"


class _RaisingKeyringBackend:
    """Keyring backend selected by ``store_provider_credential`` that then fails.

    Its cheap ``is_available()`` probe passes so the selector returns it, but
    ``create_ref`` raises a *non-degradable* ``CredentialError`` (one whose reason
    code is not ``CREDENTIAL_BACKEND_UNAVAILABLE``), so the error propagates out of
    ``store_provider_credential`` exactly as a real storage/consent fault would.
    """

    kind = "keyring"

    def is_available(self) -> bool:
        return True

    def create_ref(self, request: object) -> CredentialRef:
        raise CredentialError(
            reason_code=CREDENTIAL_REF_INVALID,
            message="keyring rejected the credential identifier",
        )


@pytest.mark.unit
def test_credential_storage_error_isolates_to_that_provider(tmp_path: Path) -> None:
    """A ``CredentialError`` from one provider's storage never aborts the others."""
    gh_ok = _SubprocessSpy(returncode=0)
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex", "github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        capture=lambda provider: _FAKE_TOKEN if provider == "codex" else None,
        run_subprocess=gh_ok,
        http_get=_unexpected_http,
        keyring_backend=_RaisingKeyringBackend(),
    )

    # Codex's storage failure is contained as an unavailable result carrying the
    # error's reason code, and GitHub is still orchestrated (the loop did not abort).
    codex = summary.result_for("codex")
    assert codex is not None
    assert codex.status == "unavailable"
    assert codex.reason_code == CREDENTIAL_REF_INVALID
    assert codex.configured is False
    github = summary.result_for("github")
    assert github is not None and github.status == "ready"
    # The failed provider leaves no fabricated config, and no secret leaks anywhere.
    assert "codex" not in config.providers
    assert _FAKE_TOKEN not in json.dumps(summary.model_dump())


# --- Selected-provider isolation -----------------------------------------


@pytest.mark.unit
def test_selected_provider_configure_does_not_touch_others(tmp_path: Path) -> None:
    """Configuring one provider leaves unselected configs and probes untouched."""
    existing = ProviderConfig(
        credential_ref="env://ANTHROPIC_API_KEY",
        backend="env_ref",
        status="ready",
        source="env",
    )
    base = HostSetupConfig(providers={"claude_code": existing})
    sub_spy = _SubprocessSpy()
    http_spy = _HttpSpy()

    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=base,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        capture=lambda provider: _FAKE_TOKEN if provider == "codex" else None,
        run_subprocess=sub_spy,
        http_get=http_spy,
        keyring_backend=_keyring_backend(),
    )

    assert summary.mode == "targeted_recheck"
    # Codex uses no subprocess/HTTP probe, so the unselected GitHub/Ollama probes
    # are proven untouched by zero injected-probe calls.
    assert sub_spy.calls == []
    assert http_spy.calls == []
    assert config.providers["claude_code"] == existing
    assert summary.result_for("github") is None
    assert summary.result_for("codex") is not None


@pytest.mark.unit
def test_selected_github_recheck_probes_only_github(tmp_path: Path) -> None:
    """Rechecking only GitHub probes GitHub and leaves other providers alone."""
    existing = ProviderConfig(
        credential_ref="env://GH_TOKEN",
        backend="env_ref",
        status="ready",
        source="env",
    )
    base = HostSetupConfig(providers={"github": existing})
    sub_spy = _SubprocessSpy(returncode=0)
    http_spy = _HttpSpy()

    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=base,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        run_subprocess=sub_spy,
        http_get=http_spy,
    )

    assert summary.mode == "targeted_recheck"
    assert len(sub_spy.calls) == 1
    assert sub_spy.calls[0][:2] == ["gh", "auth"]
    assert http_spy.calls == []
    assert set(config.providers) == {"github"}
    github = summary.result_for("github")
    assert github is not None and github.status == "ready"


# --- GitHub first-class ---------------------------------------------------


@pytest.mark.unit
def test_github_ready_via_gh_without_token(tmp_path: Path) -> None:
    """``gh auth status`` success marks GitHub ready with no stored token."""
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        run_subprocess=_SubprocessSpy(returncode=0),
        http_get=_unexpected_http,
    )

    github = summary.result_for("github")
    assert github is not None
    assert github.status == "ready"
    assert github.credential_ref is None
    assert github.backend is None
    assert config.providers["github"].status == "ready"
    assert config.providers["github"].source == "gh"


@pytest.mark.unit
def test_github_ready_via_env_ref_when_gh_absent(tmp_path: Path) -> None:
    """An env token marks GitHub ready via env ref even when ``gh`` is absent."""
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        run_subprocess=_SubprocessSpy(raise_exc=FileNotFoundError("gh")),
        http_get=_unexpected_http,
    )

    github = summary.result_for("github")
    assert github is not None
    assert github.status == "ready"
    assert github.credential_ref == "env://GH_TOKEN"
    assert github.backend == "env_ref"
    assert _FAKE_GH_TOKEN not in json.dumps(github.model_dump())
    assert config.providers["github"].credential_ref == "env://GH_TOKEN"


@pytest.mark.unit
def test_github_ready_via_awf_github_token_env_ref(tmp_path: Path) -> None:
    """``AWF_GITHUB_TOKEN`` (the documented service var) marks GitHub ready.

    Regression for a registry that only honored ``GH_TOKEN`` / ``GITHUB_TOKEN``:
    an operator who exports only ``AWF_GITHUB_TOKEN`` — the token ``awf start``
    uses for PR creation/monitoring — must not have ``awf setup`` reject GitHub.
    """
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"AWF_GITHUB_TOKEN": _FAKE_GH_TOKEN},
        run_subprocess=_SubprocessSpy(raise_exc=FileNotFoundError("gh")),
        http_get=_unexpected_http,
    )

    github = summary.result_for("github")
    assert github is not None
    assert github.status == "ready"
    assert github.credential_ref == "env://AWF_GITHUB_TOKEN"
    assert github.backend == "env_ref"
    assert _FAKE_GH_TOKEN not in json.dumps(github.model_dump())
    assert config.providers["github"].credential_ref == "env://AWF_GITHUB_TOKEN"


@pytest.mark.unit
def test_github_invalid_env_token_overwrites_prior_ready_config(tmp_path: Path) -> None:
    """A rejected GitHub token must not leave a prior ready config entry stale."""
    prior = HostSetupConfig(providers={"github": ProviderConfig(status="ready", source="gh")})
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=prior,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        run_subprocess=_SubprocessSpy(returncode=1),
        http_get=_unexpected_http,
    )

    github = summary.result_for("github")
    assert github is not None
    assert github.status == "unavailable"
    assert github.reason_code == PROVIDER_SETUP_AUTH_INVALID
    # The persisted config must agree with the summary, not retain the stale entry.
    assert config.providers["github"].status == "unavailable"
    assert config.providers["github"].credential_ref == "env://GH_TOKEN"
    assert _FAKE_GH_TOKEN not in json.dumps(config.providers["github"].model_dump())


# --- AWF Cloud stub -------------------------------------------------------


@pytest.mark.unit
def test_awf_cloud_is_a_deterministic_stub(tmp_path: Path) -> None:
    """The AWF Cloud slot returns a deterministic stub and never probes."""
    sub_spy = _SubprocessSpy()
    http_spy = _HttpSpy()
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["awf_cloud"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        run_subprocess=sub_spy,
        http_get=http_spy,
    )

    result = summary.result_for("awf_cloud")
    assert result is not None
    assert result.status == "not_configured"
    assert result.reason_code == "AWF_CLOUD_STUB"
    assert sub_spy.calls == []
    assert http_spy.calls == []
    assert "awf_cloud" not in config.providers


# --- Bounded probes -------------------------------------------------------


@pytest.mark.unit
def test_all_probes_are_bounded(tmp_path: Path) -> None:
    """Every injected subprocess/HTTP probe receives a positive timeout."""
    sub_spy = _SubprocessSpy(returncode=0)
    http_spy = _HttpSpy()
    orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github", "opencode"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=False,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN, "OLLAMA_API_KEY": "ollama-xyz"},
        run_subprocess=sub_spy,
        http_get=http_spy,
    )

    assert sub_spy.timeouts and all(timeout > 0 for timeout in sub_spy.timeouts)
    assert http_spy.timeouts and all(timeout > 0 for timeout in http_spy.timeouts)


# --- No raw secret leakage ------------------------------------------------


@pytest.mark.unit
def test_no_raw_secret_in_summary_or_results(tmp_path: Path) -> None:
    """No raw secret appears in the summary dump, details, or any result."""
    summary, _config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github", "codex"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"GH_TOKEN": _FAKE_GH_TOKEN},
        capture=lambda provider: _FAKE_TOKEN if provider == "codex" else None,
        run_subprocess=_SubprocessSpy(returncode=0),
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    rendered = json.dumps(summary.model_dump())
    details = json.dumps(render_provider_summary(summary))
    for secret in (_FAKE_GH_TOKEN, _FAKE_TOKEN):
        assert secret not in rendered
        assert secret not in details
    assert isinstance(summary, ProviderSetupSummary)


# --- Non-interactive capture-needed signal --------------------------------


@pytest.mark.unit
def test_non_interactive_capture_needed_signals_interactive(tmp_path: Path) -> None:
    """A selected secret-needing provider with no fallback signals interactive."""
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex", "claude_code"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"ANTHROPIC_API_KEY": "anthropic-xyz"},
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=_keyring_backend(),
    )

    assert summary.requires_interactive_input is True
    codex = summary.result_for("codex")
    claude = summary.result_for("claude_code")
    assert codex is not None and codex.reason_code == INTERACTIVE_INPUT_REQUIRED
    assert claude is not None and claude.status == "ready"
    assert "claude_code" in config.providers


@pytest.mark.unit
def test_github_gh_probe_failure_is_treated_as_unusable(tmp_path: Path) -> None:
    """A ``gh`` probe that raises (e.g. timeout) is bounded and non-blocking."""
    import subprocess

    spy = _SubprocessSpy(raise_exc=subprocess.TimeoutExpired(cmd="gh", timeout=5.0))
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["github"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=False,
        environ={},
        run_subprocess=spy,
        http_get=_unexpected_http,
    )

    github = summary.result_for("github")
    assert github is not None
    assert github.status == "not_configured"
    assert "github" not in config.providers


@pytest.mark.unit
def test_captured_secret_degrades_to_env_ref_without_reinjecting(tmp_path: Path) -> None:
    """When keyring is unavailable a captured secret falls back to the env ref."""
    no_keyring = KeyringCredentialBackend(keyring_module=FakeKeyringModule(backend=_FailBackend()))
    summary, config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"OPENAI_API_KEY": "sk-env-value"},
        capture=lambda provider: _FAKE_TOKEN if provider == "codex" else None,
        run_subprocess=_unexpected_subprocess,
        http_get=_unexpected_http,
        keyring_backend=no_keyring,
    )

    codex = summary.result_for("codex")
    assert codex is not None
    assert codex.status == "ready"
    assert codex.backend == "env_ref"
    assert config.providers["codex"].credential_ref == "env://OPENAI_API_KEY"


# --- All-provider run -----------------------------------------------------


@pytest.mark.unit
def test_all_provider_run_labels_all_providers_mode(tmp_path: Path) -> None:
    """An empty selection orchestrates every provider under all_providers mode."""
    summary, _config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=[],
        config=HostSetupConfig(),
        allow_plain_secrets=False,
        non_interactive=False,
        environ={},
        run_subprocess=_SubprocessSpy(returncode=1),
        http_get=_HttpSpy(healthy=False),
    )

    assert summary.mode == "all_providers"
    assert {result.name for result in summary.providers} == set(KNOWN_SETUP_PROVIDERS)
