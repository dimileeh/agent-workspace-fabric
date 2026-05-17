"""Focused coverage edges for defensive service helpers."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.models import Workspace
from awf.service import pr_monitor_adoption as adoption_module
from awf.service import supply_chain_policy as supply_chain_module
from awf.service import workspaces as workspaces_module
from awf.service.bounded_list import InvalidBoundedListCursorError
from awf.service.operations import decode_operation_list_cursor
from awf.service.supply_chain_policy import (
    _command_with_shell_payloads,
    _has_process_substitution_remote_script_execution,
    _node_package_version,
    _registry_hosts,
    _remote_fetch_output_targets,
    _shell_tokens,
)
from awf.service.workspace_observability import (
    _latest_recovery_operation,
    workspace_usage_summary,
)


def _encoded_operation_cursor(*, operation_id: object) -> str:
    payload = {
        "c": datetime(2026, 5, 8, tzinfo=UTC).isoformat(),
        "i": operation_id,
    }
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def _v2_request(*, profile_ref: str | None = "auto") -> WorkspaceCreateRequest:
    return WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/coverage.git", "base_branch": "main"},
        task={
            "title": "Cover service helper edge",
            "prompt": "Exercise defensive helper branch.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "owned_paths": [],
        },
        workspace={"profile_ref": profile_ref, "profile": None},
        validation={"commands": ["pytest -q"], "requested_tier": 1},
        resources={},
    )


@pytest.mark.unit
def test_operation_cursor_rejects_empty_operation_id() -> None:
    with pytest.raises(InvalidBoundedListCursorError):
        decode_operation_list_cursor(_encoded_operation_cursor(operation_id=""))


@pytest.mark.unit
def test_supply_chain_helpers_cover_defensive_parser_edges() -> None:
    assert not _has_process_substitution_remote_script_execution(
        _shell_tokens("bash <(curl ./local-script.sh)")
    )
    assert _registry_hosts(
        [],
        manager="pip",
        env_assignments=(
            "PIP_INDEX_URL=https://mirror.example/simple",
            "PIP_EXTRA_INDEX_URL=https://mirror.example/extra",
        ),
    ) == ["mirror.example"]
    assert _registry_hosts(
        [],
        manager="npm",
        env_assignments=(
            "NPM_CONFIG_REGISTRY=https://registry.example",
            "npm_config_registry=https://registry.example",
        ),
    ) == ["registry.example"]
    assert _registry_hosts(
        ["--index-url", "https://packages.example/simple"],
        manager="uv",
    ) == ["packages.example"]
    assert _node_package_version("alias@npm:@scope/pkg@1.2.3") == "1.2.3"
    assert _remote_fetch_output_targets(["curl", "-o"]) == []
    assert _remote_fetch_output_targets(["fetch", "-o", "artifact.sh"]) == []
    assert _remote_fetch_output_targets(["wget", "-O"]) == []
    assert _command_with_shell_payloads('bash -c "\n\n"') == ['bash -c "\n\n"']


@pytest.mark.unit
def test_registry_hosts_deduplicates_env_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supply_chain_module,
        "_pip_env_registry_hosts",
        lambda _assignments: ["mirror.example", "mirror.example"],
    )
    assert _registry_hosts([], manager="pip", env_assignments=("ignored=true",)) == [
        "mirror.example"
    ]

    monkeypatch.setattr(
        supply_chain_module,
        "_node_env_registry_hosts",
        lambda _assignments: ["registry.example", "registry.example"],
    )
    assert _registry_hosts([], manager="npm", env_assignments=("ignored=true",)) == [
        "registry.example"
    ]


@pytest.mark.unit
def test_workspace_helpers_cover_unloaded_and_snapshot_edges() -> None:
    unloaded_workspace = Workspace(id="ws_unloaded")

    with pytest.raises(RuntimeError, match="Workspace.operations must be eager-loaded"):
        workspace_usage_summary(unloaded_workspace)
    with pytest.raises(ValueError, match="operations relationship must be preloaded"):
        _latest_recovery_operation(unloaded_workspace, active_only=True)

    assert not workspaces_module._profile_ref_matches(  # noqa: SLF001
        SimpleNamespace(profile_ref=None, requested_profile=None, task_attempt=object()),
        _v2_request(profile_ref="named-profile"),
    )
    assert (
        workspaces_module._stored_resource_dind_slots(  # noqa: SLF001
            SimpleNamespace(resolved_profile={"docker": {"mode": "dind"}}),
            _v2_request(),
        )
        == 1
    )
    assert (
        workspaces_module._stored_validation_requested_tier(  # noqa: SLF001
            SimpleNamespace(
                task_policy={workspaces_module.VALIDATION_POLICY_KEY: {"requested_tier": True}},
                resolved_profile={"validation": {"requested_tier": 4}},
            )
        )
        == 4
    )

    planning_payload = workspaces_module._planning_scope_recovery_payload(  # noqa: SLF001
        workspaces_module._PlanningScopeRetryContext(  # noqa: SLF001
            reason_code="AGENT_PLAN_PHASE_SCOPE_VIOLATION",
            evidence={"summary": "scope drift"},
            evidence_ref={"source_workspace_id": "ws_old"},
            recovery_strategy="retry_without_salvage",
            salvage_policy="drop_original_diff",
            fallback_model={"agent": "codex", "model": "gpt-5.5"},
        )
    )
    assert "salvage" not in planning_payload
    assert planning_payload["fallback_model"] == {"agent": "codex", "model": "gpt-5.5"}


@pytest.mark.unit
async def test_pr_adoption_external_id_family_ignores_null_rows() -> None:
    class _Result:
        def scalars(self) -> object:
            return self

        def all(self) -> list[str | None]:
            return [None, "adopt:example/repo#12", "adopt:example/repo#12:g2"]

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    keys = await adoption_module._task_external_id_family_idempotency_keys(  # noqa: SLF001
        _Session(),  # type: ignore[arg-type]
        logical_idempotency_key="pr-adoption:example/repo#12",
        task_external_id="adopt:example/repo#12",
    )

    assert keys == ["pr-adoption:example/repo#12", "pr-adoption:example/repo#12:g2"]
