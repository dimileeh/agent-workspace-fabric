"""Diagnostics bypass ordinary feature admission, without project authority."""

import pytest
from pydantic import ValidationError

from awf.api.schemas_providers import ProviderConnectionTestRequest
from awf.service.provider_diagnostics import diagnostic_material


def request(**extra):
    return ProviderConnectionTestRequest(
        agent="codex", provider="ollama", model="unknown:cloud", **extra
    )


def test_diagnostic_authorizes_candidate_and_strips_project_profile():
    spec, profile, selection = diagnostic_material(
        request(
            profile={
                "name": "project",
                "runtime": {
                    "environment": {
                        "AWF_OLLAMA_BASE_URL": "http://daemon:11434",
                        "GH_TOKEN": "${FORGE}",
                        "OPENAI_API_KEY": "${NATIVE}",
                    }
                },
                "phases": {"setup": [{"command": "steal secrets"}]},
                "services": [{"name": "postgres", "image": "postgres:16"}],
            }
        ),
        environ={},
    )
    assert selection.model == "unknown:cloud"
    assert spec["deadline_seconds"] == 180
    assert spec["version"] == 1
    assert "GH_TOKEN" not in repr(profile)
    assert "OPENAI_API_KEY" not in repr(profile)
    assert not profile["services"]
    assert not profile["phases"]["setup"]
    assert spec["selection"] == selection.model_dump(mode="json")


def test_selected_sidecar_project_dependencies_are_explicitly_unsupported():
    with pytest.raises(ValueError, match="PROVIDER_DIAGNOSTIC_PROFILE_UNSUPPORTED"):
        diagnostic_material(
            request(
                profile={
                    "name": "project",
                    "runtime": {"environment": {"AWF_OLLAMA_BASE_URL": "http://ollama:11434"}},
                    "services": [
                        {
                            "name": "ollama",
                            "image": "ollama/ollama",
                            "volumes": [["./data", "/root/.ollama"]],
                        }
                    ],
                }
            ),
            environ={},
        )


@pytest.mark.parametrize(
    "extra", [{"deadline_seconds": 901}, {"repo": {}}, {"prompt": "arbitrary"}]
)
def test_no_project_payload_and_bounded_deadline(extra):
    with pytest.raises(ValidationError):
        request(**extra)


def test_incompatible_models_and_effort_still_fail():
    with pytest.raises(ValueError, match="MODEL_INCOMPATIBLE"):
        diagnostic_material(
            ProviderConnectionTestRequest(agent="codex", provider="ollama", model="embedding"),
            environ={},
        )
    with pytest.raises(ValueError, match="EFFORT_UNSUPPORTED"):
        diagnostic_material(request(effort="high"), environ={})


def test_native_diagnostic_never_persists_inline_credentials():
    with pytest.raises(ValueError, match="CREDENTIAL_REFERENCE_REQUIRED"):
        diagnostic_material(
            ProviderConnectionTestRequest(
                agent="codex",
                provider="openai",
                model="native",
                profile={
                    "name": "native",
                    "runtime": {"environment": {"OPENAI_API_KEY": "must-not-persist"}},
                },
            ),
            environ={},
        )
    spec, profile, _ = diagnostic_material(
        ProviderConnectionTestRequest(agent="claude_code", provider="anthropic", model="native"),
        environ={"ANTHROPIC_API_KEY": "native-private-value", "GH_TOKEN": "forge-private-value"},
    )
    assert profile["runtime"]["environment"] == {"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"}
    assert "native-private-value" not in repr(spec) and "forge-private-value" not in repr(spec)


def test_diagnostic_contract_and_cleanup_reasons_are_registered():
    from awf.service.doctor.reasons import _REASON_TEXT

    for code in (
        "PROVIDER_DIAGNOSTIC_CONTRACT_UNSUPPORTED",
        "PROVIDER_DIAGNOSTIC_CLEANUP_FAILED",
        "PROVIDER_DIAGNOSTIC_INTERRUPTED",
        "PROVIDER_DIAGNOSTIC_PASSED",
    ):
        assert code in _REASON_TEXT
        assert _REASON_TEXT[code].message
        assert _REASON_TEXT[code].action


def test_native_declared_credentials_keep_only_selected_references():
    request = ProviderConnectionTestRequest(
        agent="codex",
        provider="openai",
        model="native",
        profile={
            "name": "native",
            "secrets": [
                {
                    "name": "native",
                    "kind": "env",
                    "provider": "env",
                    "ref": "env/NATIVE_ALIAS",
                    "target": "OPENAI_API_KEY",
                },
                {
                    "name": "forge",
                    "kind": "env",
                    "provider": "env",
                    "ref": "env/FORGE_ALIAS",
                    "target": "GH_TOKEN",
                },
            ],
        },
    )
    spec, profile, _ = diagnostic_material(
        request,
        environ={"NATIVE_ALIAS": "private-native-value", "FORGE_ALIAS": "private-forge-value"},
    )
    assert profile["runtime"]["environment"] == {"OPENAI_API_KEY": "${NATIVE_ALIAS}"}
    assert "private-native-value" not in repr(spec) and "private-forge-value" not in repr(spec)
    assert "GH_TOKEN" not in repr(profile)
