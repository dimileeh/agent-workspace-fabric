"""Runtime health classification tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.db.enums import WorkspaceStatus
from awf.runtime.inspection import RuntimeSnapshot
from awf.service.workspace_runtime_health import (
    RuntimeResource,
    RuntimeWorkspace,
    _metadata_finding,
    classify_resource_inventory,
    classify_runtime_snapshot,
    has_open_pr_for_remonitor,
    retry_policy_allows_runtime_recovery,
    runtime_resource_from_detected,
)


@pytest.mark.unit
def test_detected_runtime_resource_ignores_non_mapping_detail() -> None:
    resource = SimpleNamespace(
        kind="container",
        detail="docker output without parsed labels",
        workspace_id="",
        compose_project="awf_ws",
        id="container-123",
    )

    normalized = runtime_resource_from_detected(resource)

    assert normalized.resource_kind == "container"
    assert normalized.workspace_id is None
    assert normalized.compose_project == "awf_ws"
    assert normalized.service is None
    assert normalized.state is None
    assert normalized.container_id == "container-123"


@pytest.mark.unit
def test_runtime_snapshot_ignores_inactive_workspace_and_reports_missing_metadata() -> None:
    snapshot = RuntimeSnapshot(stack_state="stopped")

    assert (
        classify_runtime_snapshot(
            RuntimeWorkspace(
                workspace_id="ws_completed",
                status=WorkspaceStatus.completed.value,
            ),
            snapshot,
        )
        is None
    )

    finding = classify_runtime_snapshot(
        RuntimeWorkspace(
            workspace_id="ws_ready",
            status=WorkspaceStatus.ready.value,
        ),
        snapshot,
    )

    assert finding is not None
    assert finding.reason_code == "STRANDED_WORKSPACE"
    assert finding.decision == "fail_workspace"


@pytest.mark.unit
def test_resource_inventory_skips_pre_provisioned_request_without_metadata() -> None:
    requested = RuntimeWorkspace(
        workspace_id="ws_requested",
        status=WorkspaceStatus.requested.value,
    )

    assert classify_resource_inventory(requested, resources=()) is None
    assert _metadata_finding(requested) is None


@pytest.mark.unit
def test_open_pr_remonitor_predicate_accepts_model_and_candidate_statuses() -> None:
    assert has_open_pr_for_remonitor(
        WorkspaceStatus.monitoring_pr.value,
        "https://github.com/example/repo/pull/123",
    )
    assert has_open_pr_for_remonitor(
        WorkspaceStatus.monitoring_pr,
        "https://github.com/example/repo/pull/123",
    )

    assert not has_open_pr_for_remonitor(
        WorkspaceStatus.monitoring_pr.value,
        None,
    )
    assert not has_open_pr_for_remonitor(
        WorkspaceStatus.running,
        "https://github.com/example/repo/pull/123",
    )


@pytest.mark.unit
def test_resource_inventory_matches_by_compose_project_and_policy_must_request_retry() -> None:
    workspace = RuntimeWorkspace(
        workspace_id="ws_running",
        status=WorkspaceStatus.running.value,
        compose_project_name="awf_ws_running",
    )
    resources = (
        RuntimeResource(
            resource_kind="container",
            workspace_id=None,
            compose_project="awf_ws_running",
            service="agent",
            state="running",
            container_id="agent-1",
        ),
    )

    assert classify_resource_inventory(workspace, resources) is None
    assert (
        retry_policy_allows_runtime_recovery({"runtime_recovery": {"stranded_workspace": "manual"}})
        is False
    )
    assert retry_policy_allows_runtime_recovery({"runtime_recovery": "manual"}) is False
