"""Focused tests for managed companion service conversion helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.companion_services import (
    MaterializedCompanionService,
    WorkspaceCompanionSpec,
    companion_service_from_materialized,
    companion_specs_from_task_policy,
    validate_companion_service_graph,
)
from awf.node.compose_manager import ComposeService
from awf.node.git_manager import WorktreeLayout
from awf.profiles.models import DockerMode
from awf.profiles.resolver import ProfileResolutionError


def _layout(root: Path) -> WorktreeLayout:
    return WorktreeLayout(
        mirror_path=root.parent / "mirror.git",
        worktree_path=root,
        branch_name="awf/ws_parent/companion/backend",
    )


@pytest.mark.unit
def test_companion_specs_from_task_policy_normalizes_optional_values(
    tmp_path: Path,
) -> None:
    companion_root = tmp_path / "backend"
    (companion_root / "fixtures").mkdir(parents=True)
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment": "not-a-mapping",
                    "depends_on": ["db", 3],
                    "ports": [[8000, 18000]],
                    "volumes": [["cache-volume", "/cache"], ["./fixtures", "/fixtures"]],
                }
            ]
        }
    )[0]

    assert spec.environment == ()
    assert spec.depends_on == ("db",)
    assert spec.ports == ((8000, 18000),)

    service = companion_service_from_materialized(
        MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
    )

    assert service.volumes == (
        ("cache-volume", "/cache"),
        (str(companion_root / "fixtures"), "/fixtures"),
    )


@pytest.mark.unit
def test_companion_specs_from_task_policy_stringifies_environment_values() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "environment": {"DEBUG": True, "PORT": 8000},
                }
            ]
        }
    )[0]

    assert spec.environment == (("DEBUG", "True"), ("PORT", "8000"))


@pytest.mark.unit
def test_companion_specs_from_task_policy_treats_null_optional_sequences_as_empty() -> None:
    spec = companion_specs_from_task_policy(
        {
            "companions": [
                {
                    "name": "backend",
                    "repo_url": "git@example.com:api.git",
                    "base_branch": "main",
                    "depends_on": None,
                    "ports": None,
                    "volumes": None,
                }
            ]
        }
    )[0]

    assert spec.depends_on == ()
    assert spec.ports == ()
    assert spec.volumes == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "companion",
    [
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
            "base_branch": "main",
            "ports": ["bad"],
        },
        {
            "name": "backend",
            "repo_url": "git@example.com:api.git",
            "base_branch": "main",
            "volumes": ["bad"],
        },
    ],
)
def test_companion_specs_from_task_policy_rejects_malformed_sequences(
    companion: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="two-item sequences"):
        companion_specs_from_task_policy({"companions": [companion]})


@pytest.mark.unit
def test_companion_service_from_materialized_rejects_escaping_paths(tmp_path: Path) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    spec = WorkspaceCompanionSpec(
        name="backend",
        repo_url="git@example.com:api.git",
        base_branch="main",
        build_context="../outside",
    )

    with pytest.raises(ValueError, match="escapes managed worktree"):
        companion_service_from_materialized(
            MaterializedCompanionService(spec=spec, layout=_layout(companion_root))
        )


@pytest.mark.unit
def test_validate_companion_service_graph_rejects_unknown_dependencies(tmp_path: Path) -> None:
    companion_root = tmp_path / "backend"
    companion_root.mkdir()
    companion = MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(
            name="backend",
            repo_url="git@example.com:api.git",
            base_branch="main",
            depends_on=("missing-companion-target",),
        ),
        layout=_layout(companion_root),
    )

    with pytest.raises(ProfileResolutionError) as exc:
        validate_companion_service_graph(
            profile_services=(ComposeService(name="web", depends_on=("missing-profile-target",)),),
            companions=(companion,),
            docker_mode=DockerMode.none,
        )

    assert exc.value.reason_code == "COMPANION_SERVICE_DEPENDENCY_UNKNOWN"
    assert "backend->missing-companion-target" in str(exc.value)
    assert "web->missing-profile-target" in str(exc.value)
