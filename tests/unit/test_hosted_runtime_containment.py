"""Trusted hosted containment is rendered; the ordinary local default is absent."""

from pathlib import Path

import yaml

from awf.node.compose_manager import ComposeManager, ComposeService, WorkspaceComposeSpec


def test_cgroup_parent_applies_to_agent_and_every_profile_service(tmp_path):
    template = Path(__file__).resolve().parents[2] / "docker/compose/workspace.base.yml.j2"
    manager = ComposeManager(
        work_dir=tmp_path, template_path=template, cgroup_parent="/awf-execution"
    )
    spec = WorkspaceComposeSpec(
        workspace_id="ws_test",
        worktree_host_path=tmp_path / "checkout",
        services=(ComposeService(name="postgres", image="postgres:16"),),
    )
    manifest = yaml.safe_load(manager.render(spec).compose_file.read_text())
    assert {item["cgroup_parent"] for item in manifest["services"].values()} == {"/awf-execution"}
    local = ComposeManager(work_dir=tmp_path / "local", template_path=template)
    unchanged = yaml.safe_load(local.render(spec).compose_file.read_text())
    assert all("cgroup_parent" not in item for item in unchanged["services"].values())
