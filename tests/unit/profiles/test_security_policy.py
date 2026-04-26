import pytest
from pydantic import ValidationError

from awf.profiles.models import WorkspaceProfile


@pytest.mark.unit
def test_valid_egress_open():
    profile = WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "open"}}})
    assert profile.security.egress.mode == "open"

@pytest.mark.unit
def test_valid_egress_offline():
    profile = WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "offline"}}})
    assert profile.security.egress.mode == "offline"

@pytest.mark.unit
def test_valid_egress_mirrored():
    profile = WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "mirrored"}}})
    assert profile.security.egress.mode == "mirrored"

@pytest.mark.unit
def test_valid_egress_allowlist():
    profile = WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "allowlist", "allowlist": ["api.github.com"]}}})
    assert profile.security.egress.mode == "allowlist"
    assert profile.security.egress.allowlist == ["api.github.com"]

@pytest.mark.unit
def test_inconsistent_egress_allowlist_missing():
    with pytest.raises(ValidationError, match="allowlist must be populated"):
        WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "allowlist"}}})

@pytest.mark.unit
def test_inconsistent_egress_allowlist_when_open():
    with pytest.raises(ValidationError, match="allowlist cannot be populated"):
        WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "open", "allowlist": ["api.github.com"]}}})

@pytest.mark.unit
def test_inconsistent_egress_allowlist_when_offline():
    with pytest.raises(ValidationError, match="allowlist cannot be populated"):
        WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "offline", "allowlist": ["api.github.com"]}}})

@pytest.mark.unit
def test_invalid_allowlist_entries():
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "allowlist", "allowlist": ["*"]}}})

@pytest.mark.unit
def test_valid_egress_mirrored_with_allowlist():
    profile = WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "mirrored", "allowlist": ["api.github.com"]}}})
    assert profile.security.egress.mode == "mirrored"
    assert profile.security.egress.allowlist == ["api.github.com"]

@pytest.mark.unit
def test_broad_secret_targets():
    with pytest.raises(ValidationError, match="too broad"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/", "kind": "mount"}]})

@pytest.mark.unit
def test_raw_looking_secret_values():
    with pytest.raises(ValidationError, match="raw secret value"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/etc/s1", "provider": "aws", "ref": "sk-1234567890abcdef"}]})

@pytest.mark.unit
def test_missing_provider_or_ref():
    with pytest.raises(ValidationError, match="secret 'ref' must be provided if 'provider' is specified"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/etc/s1", "provider": "aws"}]})
    with pytest.raises(ValidationError, match="secret 'provider' must be provided if 'ref' is specified"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/etc/s1", "ref": "my-secret"}]})

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
                "mode": "allowlist",
                "allowlist": ["api.github.com", "crates.io"],
            }
        },
    })
    dumped = profile.model_dump(mode="json")
    assert "secrets" in dumped
    assert len(dumped["secrets"]) == 1
    assert dumped["secrets"][0]["name"] == "API_KEY"
    assert dumped["secrets"][0]["provider"] == "aws"
    assert "security" in dumped
    assert dumped["security"]["egress"]["mode"] == "allowlist"
    assert dumped["security"]["egress"]["allowlist"] == ["api.github.com", "crates.io"]
