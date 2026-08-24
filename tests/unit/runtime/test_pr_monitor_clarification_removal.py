"""Regression contract for removing the verdict clarification sidecar."""

from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from awf.adapters.base import AgentAdapter
from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.runtime import ownership

_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.mark.unit
def test_workspace_compose_contract_has_no_clarification_resources(tmp_path: Path) -> None:
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    spec = WorkspaceComposeSpec(
        workspace_id="ws_no_clarification",
        worktree_host_path=tmp_path / "checkout",
        postgres_password="deterministic-test-password",
    )

    rendered = yaml.safe_load(manager.render(spec).compose_file.read_text(encoding="utf-8"))
    assert not any("clarification" in name for name in rendered["services"])
    assert not any("clarification" in name for name in rendered["networks"])
    assert not any("clarification" in field.name for field in fields(WorkspaceComposeSpec))


@pytest.mark.unit
def test_agent_execution_contract_has_no_reask_only_arguments() -> None:
    run_parameters = inspect.signature(AgentAdapter.run).parameters
    assert "isolated_worktree_host_path" not in run_parameters
    assert "isolated_worktree_ref" not in run_parameters
    assert "isolated_worktree_source_mirror" not in run_parameters
    assert "read_only" not in run_parameters
    assert "read_only" not in {field.name for field in fields(AgentRuntimeExecRequest)}


@pytest.mark.unit
def test_runtime_ownership_contract_has_no_reask_only_surface() -> None:
    assert not hasattr(ownership, "validated_source_worktree_git_context")
    repair_parameters = inspect.signature(ownership.repair_agent_runtime_ownership).parameters
    assert "linked_worktree_id" not in repair_parameters
    assert "repair_shared_git_metadata" not in repair_parameters
