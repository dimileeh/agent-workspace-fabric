"""Workspace profile model, resolver, and detector tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from awf.profiles.models import DockerMode, WorkspaceProfile
from awf.profiles.registry import aira_profile, docker_compose_profile
from awf.profiles.resolver import ProfileResolver


@pytest.mark.unit
def test_profile_schema_accepts_minimal_valid_profile() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "go-explicit",
            "docker": {"mode": "none"},
            "phases": {"setup": ["go mod download"], "validate": ["go test ./..."]},
        }
    )
    assert profile.name == "go-explicit"
    assert profile.phases.setup[0].command == "go mod download"
    assert profile.phases.validate_commands[0].command == "go test ./..."


@pytest.mark.unit
def test_profile_schema_rejects_service_without_image_or_build() -> None:
    with pytest.raises(ValidationError):
        WorkspaceProfile.model_validate(
            {
                "name": "bad",
                "services": [{"name": "db"}],
            }
        )


@pytest.mark.unit
def test_resolution_precedence_inline_beats_repo_and_registry(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text(
        "name: repo-profile\nphases:\n  validate:\n    - pytest\n",
        encoding="utf-8",
    )
    resolver = ProfileResolver()
    result = resolver.resolve(
        worktree_path=tmp_path,
        inline_profile={"name": "inline-profile", "phases": {"validate": ["echo inline"]}},
        profile_ref="python",
    )
    assert result.profile.name == "inline-profile"
    assert result.reason == "inline profile supplied by request"


@pytest.mark.unit
def test_repo_profile_beats_registry(tmp_path: Path) -> None:
    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text(
        "name: repo-profile\nphases:\n  validate:\n    - pytest\n",
        encoding="utf-8",
    )
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="python")
    assert result.profile.name == "repo-profile"
    assert result.profile.source == "repo:.awf/workspace.yml"


@pytest.mark.unit
def test_auto_detection_prefers_docker_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "docker-compose"
    assert result.profile.docker.mode == DockerMode.dind


@pytest.mark.unit
def test_auto_detection_detects_nextjs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "latest"}, "scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "nextjs"
    assert result.profile.phases.setup[0].command == "pnpm install --frozen-lockfile"


@pytest.mark.unit
def test_auto_detection_falls_back_to_generic(tmp_path: Path) -> None:
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "generic"
    assert result.profile.confidence == "low"


@pytest.mark.unit
def test_docker_compose_builtin_enables_dind() -> None:
    profile = docker_compose_profile()
    assert profile.docker.mode == DockerMode.dind
    assert profile.runtime.environment["DOCKER_HOST"] == "tcp://docker:2375"
    assert profile.phases.setup[0].command == "docker compose up -d --wait"


@pytest.mark.unit
def test_aira_profile_keeps_project_specific_bits_out_of_base_stack() -> None:
    profile = aira_profile()
    assert profile.services[0].name == "postgres"
    assert profile.services[0].image == "pgvector/pgvector:pg18"
    assert "AIRA_DATABASE_URL" in profile.runtime.environment
    assert "alembic upgrade head" in [c.command for c in profile.phases.setup]
