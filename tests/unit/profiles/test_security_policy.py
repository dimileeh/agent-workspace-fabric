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
def test_invalid_allowlist_entries():
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate({"name": "test", "security": {"egress": {"mode": "allowlist", "allowlist": ["*"]}}})

@pytest.mark.unit
def test_broad_secret_targets():
    with pytest.raises(ValidationError, match="too broad"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/", "kind": "mount"}]})

@pytest.mark.unit
def test_raw_looking_secret_values():
    with pytest.raises(ValidationError, match="raw secret value"):
        WorkspaceProfile.model_validate({"name": "test", "secrets": [{"name": "s1", "target": "/etc/s1", "ref": "sk-1234567890abcdef"}]})

