"""Hosted create is a bounded association, never request-driven activation."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from awf.api.schemas import WorkspaceCreateRequest
from awf.hosted_node.config import HostedSettings


def hosted_create() -> dict:
    def binding(role: str, target: str) -> dict:
        return {
            "identity": {
                "owner": "legacy-owner/nodea",
                "incarnation": "8063738394745155541",
                "recovery_generation": 1,
                "boot_epoch": 2,
            },
            "namespace": "tenant",
            "workspace_id": "ws_cloud_stable",
            "preparation_id": role,
            "purpose": "credential",
            "target": target,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "idempotency_key": role,
            "grant_id": role,
            "revision": 1,
            "authorization_generation": 1,
            "fence": 1,
        }

    return {
        "repo": {"url": "https://github.com/example/repo.git", "base_branch": "main"},
        "task": {"title": "Synthetic", "prompt": "Synthetic", "agent": "codex"},
        "hosted": {
            "workspace_id": "ws_cloud_stable",
            "credentials": {
                "agent": binding("agent", "/run/awf/secrets/openai.api-key"),
                "github": binding("github", "/run/awf/secrets/github.api-key"),
            },
        },
    }


def test_hosted_create_accepts_actual_stable_workspace_and_existing_bindings():
    value = WorkspaceCreateRequest.model_validate(hosted_create())
    assert value.hosted.workspace_id == "ws_cloud_stable"
    assert value.hosted.credentials.agent.identity.owner == "legacy-owner/nodea"


@pytest.mark.parametrize(
    "change", ["prefix", "workspace", "identity", "generation", "missing", "extra"]
)
def test_hosted_create_rejects_unbound_or_extraneous_authority(change):
    value = hosted_create()
    if change == "prefix":
        value["hosted"]["workspace_id"] = "cw_wrong"
    elif change == "workspace":
        value["hosted"]["credentials"]["agent"]["workspace_id"] = "ws_other"
    elif change == "identity":
        value["hosted"]["credentials"]["github"]["identity"]["boot_epoch"] = 3
    elif change == "generation":
        value["hosted"]["credentials"]["github"]["authorization_generation"] = 2
    elif change == "missing":
        del value["hosted"]["credentials"]["github"]
    else:
        value["hosted"]["runtime_enabled"] = True
    with pytest.raises(ValidationError):
        WorkspaceCreateRequest.model_validate(value)


def test_runtime_config_defaults_closed_and_requires_physical_lifecycle(tmp_path):
    base = {
        "owner": "owner",
        "incarnation": "123",
        "recovery_generation": 1,
        "controller_token": "synthetic-controller",
        "material_root": tmp_path,
    }
    assert HostedSettings(**base).runtime_enabled is False
    with pytest.raises(ValidationError, match="RUNTIME_HOST_LIFECYCLE_REQUIRED"):
        HostedSettings(**base, runtime_enabled=True)
