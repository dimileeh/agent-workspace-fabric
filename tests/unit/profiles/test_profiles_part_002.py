"""Workspace profile model, resolver, and detector tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.profiles.models import DockerMode
from awf.profiles.registry import (
    aira_profile,
    detect_profile,
    docker_compose_profile,
    get_builtin_profile,
    java_profile,
)


@pytest.mark.unit
def test_docker_compose_builtin_enables_dind() -> None:
    profile = docker_compose_profile()
    assert profile.docker.mode == DockerMode.dind
    assert profile.runtime.environment["DOCKER_HOST"] == "tcp://docker:2375"
    assert profile.phases.setup[0].command == "docker compose up -d --wait"


@pytest.mark.unit
def test_java_builtin_uses_worktree_build_tool_detection(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")

    profile = get_builtin_profile("java", worktree_path=tmp_path)

    assert profile is not None
    assert profile.phases.setup[0].command.startswith("./mvnw")


@pytest.mark.unit
def test_java_builtin_without_detected_build_tool_uses_default_profile(tmp_path: Path) -> None:
    profile = get_builtin_profile("java", worktree_path=tmp_path)

    assert profile is not None
    assert profile.phases.setup[0].command == "mvn -B -DskipTests dependency:go-offline"


@pytest.mark.unit
@pytest.mark.parametrize(
    "build_tool",
    ["maven", "maven-wrapper", "gradle", "gradle-wrapper"],
)
def test_java_profile_declares_jdk_toolchains(build_tool: str) -> None:
    """Every java build-tool variant declares its JDK needs (issue #527, item 6)."""
    profile = java_profile(build_tool=build_tool)

    assert profile.runtime.toolchains == {"java": ["17", "21"]}


@pytest.mark.unit
def test_java_builtin_resolution_carries_jdk_toolchains(tmp_path: Path) -> None:
    profile = get_builtin_profile("java", worktree_path=tmp_path)

    assert profile is not None
    assert profile.runtime.toolchains == {"java": ["17", "21"]}


@pytest.mark.unit
def test_aira_detector_ignores_unreadable_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("name = 'aira-agent'\n", encoding="utf-8")
    original_read_text = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == pyproject:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    profile = detect_profile(tmp_path)

    assert profile is not None
    assert profile.name == "python"


@pytest.mark.unit
def test_aira_profile_keeps_project_specific_bits_out_of_base_stack() -> None:
    profile = aira_profile()
    assert profile.services[0].name == "postgres"
    assert profile.services[0].image == "pgvector/pgvector:pg18"
    assert "AIRA_DATABASE_URL" in profile.runtime.environment
    assert "alembic upgrade head" in [c.command for c in profile.phases.setup]
    assert profile.validation.alembic.enabled is True
