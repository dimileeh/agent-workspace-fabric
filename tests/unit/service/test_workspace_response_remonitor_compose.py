"""Workspace response projection for remonitor Compose availability."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.db.enums import WorkspaceStatus
from awf.service.workspaces import workspace_response


def _workspace(**overrides: object) -> SimpleNamespace:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "id": "ws_remonitor_compose",
        "status": WorkspaceStatus.failed.value,
        "version": 1,
        "repo_url": "git@github.com:example/project.git",
        "branch_base": "main",
        "branch_name": "awf/ws_remonitor_compose",
        "remote_push_branch": "awf/ws_remonitor_compose",
        "base_commit": "abc123",
        "task_title": "Remonitor compose availability",
        "task_prompt": "Expose compose remonitor runtime availability.",
        "task_kind": "feature_branch_pr",
        "task_external_id": None,
        "task_class": None,
        "owned_paths": [],
        "task_policy": {},
        "auto_merge": True,
        "initial_review_grace_period_seconds": None,
        "agent": "codex",
        "env_profile": None,
        "profile_ref": None,
        "requested_profile": None,
        "resolved_profile": None,
        "test_commands": [],
        "requires_database": False,
        "node_id": "local",
        "compose_project_name": "awf_ws_remonitor_compose",
        "compose_file_path": None,
        "pr_url": "https://github.com/example/project/pull/1",
        "pr_number": 1,
        "failure_reason": None,
        "failure_message": None,
        "active_policy_findings": [],
        "operations": [],
        "events": [],
        "secret_leases": [],
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_workspace_response_reports_compose_remonitor_runtime_unavailable(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "removed" / "compose.yml"
    response = workspace_response(
        _workspace(compose_file_path=str(missing))  # type: ignore[arg-type]
    )

    assert response.compose_file_path == str(missing)
    assert response.remonitor_compose_runtime_available is False


@pytest.mark.unit
def test_workspace_response_reports_compose_remonitor_runtime_available(
    tmp_path: Path,
) -> None:
    present = tmp_path / "compose.yml"
    present.write_text("services: {}\n", encoding="utf-8")
    response = workspace_response(
        _workspace(compose_file_path=str(present))  # type: ignore[arg-type]
    )

    assert response.remonitor_compose_runtime_available is True
