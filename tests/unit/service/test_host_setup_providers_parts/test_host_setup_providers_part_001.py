"""Host setup provider orchestration tests (part 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.host_setup.config import HostSetupConfig, ProviderConfig
from awf.host_setup.providers import orchestrate_provider_setup
from tests.unit.service.test_host_setup_credentials_parts._helpers import _FAKE_TOKEN
from tests.unit.service.test_host_setup_providers import (
    _HttpSpy,
    _settings,
    _SubprocessSpy,
)


@pytest.mark.unit
def test_retired_gemini_keyring_degraded_during_antigravity_recheck(tmp_path: Path) -> None:
    """Targeted antigravity recheck preserves degraded unavailable status for legacy gemini keyring entry."""
    legacy_config = HostSetupConfig(
        providers={
            "gemini": ProviderConfig(
                credential_ref="keyring://gemini/api_key",
                backend="keyring",
                source="keyring",
                status="ready",
            )
        }
    )
    summary, updated_config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["antigravity"],
        config=legacy_config,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        run_subprocess=_SubprocessSpy(returncode=1),
        http_get=_HttpSpy(healthy=False),
    )

    assert "gemini" not in updated_config.providers
    assert "antigravity" in updated_config.providers
    assert updated_config.providers["antigravity"].status == "unavailable"
    ag_result = summary.result_for("antigravity")
    assert ag_result is not None
    assert ag_result.status == "unavailable"


@pytest.mark.unit
def test_retired_gemini_keyring_degraded_during_full_setup(tmp_path: Path) -> None:
    """Full setup run preserves degraded unavailable status for legacy gemini keyring entry."""
    legacy_config = HostSetupConfig(
        providers={
            "gemini": ProviderConfig(
                credential_ref="keyring://gemini/api_key",
                backend="keyring",
                source="keyring",
                status="ready",
            )
        }
    )
    summary, updated_config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=[],
        config=legacy_config,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={},
        run_subprocess=_SubprocessSpy(returncode=1),
        http_get=_HttpSpy(healthy=False),
    )

    assert "gemini" not in updated_config.providers
    assert "antigravity" in updated_config.providers
    assert updated_config.providers["antigravity"].status == "unavailable"
    ag_result = summary.result_for("antigravity")
    assert ag_result is not None
    assert ag_result.status == "unavailable"


@pytest.mark.unit
def test_unselected_unknown_provider_preserved_during_targeted_recheck(tmp_path: Path) -> None:
    """Unregistered or extension provider keys are preserved in config during targeted rechecks."""
    config_with_unknown = HostSetupConfig(
        providers={
            "future_extension": ProviderConfig(
                credential_ref="env://FUTURE_KEY",
                backend="env_ref",
                source="env",
                status="ready",
            )
        }
    )
    summary, updated_config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=config_with_unknown,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"OPENAI_API_KEY": _FAKE_TOKEN},
        run_subprocess=_SubprocessSpy(returncode=0),
        http_get=_HttpSpy(healthy=False),
    )

    assert summary.mode == "targeted_recheck"
    assert "future_extension" in updated_config.providers
    assert updated_config.providers["future_extension"].credential_ref == "env://FUTURE_KEY"
    assert "codex" in updated_config.providers
    assert updated_config.providers["codex"].status == "ready"


@pytest.mark.unit
def test_existing_antigravity_keyring_preserved_without_gemini_api_key(
    tmp_path: Path,
) -> None:
    """Existing canonical antigravity keyring entry is preserved when GEMINI_API_KEY is not in env."""
    config = HostSetupConfig(
        providers={
            "antigravity": ProviderConfig(
                credential_ref="keyring://awf/antigravity",
                backend="keyring",
                source="captured",
                status="ready",
            )
        }
    )
    summary, updated_config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=config,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"OPENAI_API_KEY": _FAKE_TOKEN},
        run_subprocess=_SubprocessSpy(returncode=0),
        http_get=_HttpSpy(healthy=False),
    )

    assert summary.mode == "targeted_recheck"
    assert "antigravity" in updated_config.providers
    assert updated_config.providers["antigravity"].backend == "keyring"
    assert updated_config.providers["antigravity"].status == "ready"


@pytest.mark.unit
def test_existing_antigravity_invalid_env_ref_degraded(tmp_path: Path) -> None:
    """Existing canonical antigravity entry with retired env ref (e.g. ANTIGRAVITY_API_KEY) is degraded."""
    config = HostSetupConfig(
        providers={
            "antigravity": ProviderConfig(
                credential_ref="env://ANTIGRAVITY_API_KEY",
                backend="env_ref",
                source="env",
                status="ready",
            )
        }
    )
    summary, updated_config = orchestrate_provider_setup(
        _settings(tmp_path),
        selected_providers=["codex"],
        config=config,
        allow_plain_secrets=False,
        non_interactive=True,
        environ={"OPENAI_API_KEY": _FAKE_TOKEN, "ANTIGRAVITY_API_KEY": _FAKE_TOKEN},
        run_subprocess=_SubprocessSpy(returncode=0),
        http_get=_HttpSpy(healthy=False),
    )

    assert summary.mode == "targeted_recheck"
    assert "antigravity" in updated_config.providers
    assert updated_config.providers["antigravity"].credential_ref == "env://ANTIGRAVITY_API_KEY"
    assert updated_config.providers["antigravity"].status == "unavailable"
