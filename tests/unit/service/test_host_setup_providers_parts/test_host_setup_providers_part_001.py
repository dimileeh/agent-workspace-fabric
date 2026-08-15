"""Host setup provider orchestration tests (part 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.host_setup.config import HostSetupConfig, ProviderConfig
from awf.host_setup.providers import orchestrate_provider_setup
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
