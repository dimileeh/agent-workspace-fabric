import pytest
from pydantic import ValidationError

from awf.profiles.lint import profile_lint_errors
from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["restricted", "offline", "open"])
def test_valid_network_postures(mode: str):
    profile = WorkspaceProfile.model_validate(
        {"name": "test", "security": {"egress": {"mode": mode}}}
    )

    assert profile.security.egress.mode == mode


@pytest.mark.unit
def test_minimal_profile_defaults_to_restricted_network_posture():
    profile = WorkspaceProfile.model_validate({"name": "test"})

    assert profile.security.egress.mode == "restricted"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["allowlist", "mirrored", "bogus"])
def test_invalid_and_legacy_network_postures_are_rejected(mode: str):
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {"name": "test", "security": {"egress": {"mode": mode}}}
        )


@pytest.mark.unit
def test_allowlist_metadata_is_not_accepted_as_enforced_posture():
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "test",
                "security": {
                    "egress": {
                        "mode": "restricted",
                        "allowlist": ["api.github.com"],
                    }
                },
            }
        )

@pytest.mark.unit
def test_broad_secret_targets():
    profile = WorkspaceProfile.model_validate(
        {"name": "test", "secrets": [{"name": "s1", "target": "/", "kind": "mount"}]}
    )
    assert profile_lint_errors(profile)[0].reason_code == "SECRET_TARGET_TOO_BROAD"

@pytest.mark.unit
def test_raw_looking_secret_values():
    profile = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [
                {
                    "name": "s1",
                    "target": "/run/awf/secrets/s1",
                    "provider": "aws",
                    "ref": "sk-1234567890abcdef",
                }
            ],
        }
    )
    assert profile_lint_errors(profile)[0].reason_code == "SECRET_REF_LOOKS_RAW"

@pytest.mark.unit
def test_missing_provider_or_ref():
    provider_only = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [{"name": "s1", "target": "/run/awf/secrets/s1", "provider": "aws"}],
        }
    )
    ref_only = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [{"name": "s1", "target": "/run/awf/secrets/s1", "ref": "my-secret"}],
        }
    )

    assert profile_lint_errors(provider_only)[0].reason_code == "SECRET_PROVIDER_REF_MISMATCH"
    assert profile_lint_errors(ref_only)[0].reason_code == "SECRET_PROVIDER_REF_MISMATCH"

@pytest.mark.unit
def test_profile_security_serialization():
    profile = WorkspaceProfile.model_validate({
        "name": "test",
        "secrets": [
            {
                "name": "API_KEY",
                "target": "API_KEY",
                "kind": "env",
                "required": True,
                "provider": "aws",
                "ref": "my-secret-id",
            }
        ],
        "security": {
            "egress": {
                "mode": "restricted",
            }
        },
    })
    dumped = profile.model_dump(mode="json")
    assert "secrets" in dumped
    assert len(dumped["secrets"]) == 1
    assert dumped["secrets"][0]["name"] == "API_KEY"
    assert dumped["secrets"][0]["provider"] == "aws"
    assert "security" in dumped
    assert dumped["security"]["egress"]["mode"] == "restricted"
