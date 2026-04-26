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
    assert profile.monitor.initial_review_grace_period_seconds == 900
    assert profile.phases.setup[0].command == "go mod download"
    assert profile.phases.validate_commands[0].command == "go test ./..."


@pytest.mark.unit
def test_profile_schema_accepts_monitor_initial_review_grace() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "python-explicit",
            "monitor": {"initial_review_grace_period_seconds": 120},
        }
    )
    assert profile.monitor.initial_review_grace_period_seconds == 120


@pytest.mark.unit
def test_profile_schema_accepts_validation_coverage_policy() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "awf-self",
            "validation": {"coverage": {"minimum_percent": 99}},
        }
    )

    assert profile.validation.coverage.minimum_percent == 99


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
@pytest.mark.parametrize(
    ("profile_ref", "setup_command", "validate_commands"),
    [
        ("go", "go mod download", ["go test ./..."]),
        ("rust", "cargo fetch", ["cargo test --all-targets"]),
        (
            "java",
            "mvn -B -DskipTests dependency:go-offline",
            ["mvn -B test"],
        ),
        (
            "cpp",
            "cmake -S . -B build",
            ["cmake --build build", "ctest --test-dir build --output-on-failure"],
        ),
    ],
)
def test_language_profiles_can_be_selected_from_registry(
    profile_ref: str, setup_command: str, validate_commands: list[str]
) -> None:
    result = ProfileResolver().resolve(worktree_path=None, profile_ref=profile_ref)
    assert result.profile.name == profile_ref
    assert result.profile.confidence == "medium"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == validate_commands


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker", "contents", "setup_command", "validate_command"),
    [
        (
            "mvnw",
            "#!/bin/sh\n",
            "./mvnw -B -DskipTests dependency:go-offline",
            "./mvnw -B test",
        ),
        (
            "build.gradle",
            "plugins { id 'java' }\n",
            "gradle --no-daemon dependencies",
            "gradle --no-daemon test",
        ),
        (
            "gradlew",
            "#!/bin/sh\n",
            "./gradlew --no-daemon dependencies",
            "./gradlew --no-daemon test",
        ),
    ],
)
def test_explicit_java_registry_profile_uses_detected_build_tool(
    tmp_path: Path,
    marker: str,
    contents: str,
    setup_command: str,
    validate_command: str,
) -> None:
    (tmp_path / marker).write_text(contents, encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="java")

    assert result.reason == "central registry profile java"
    assert result.profile.name == "java"
    assert result.profile.source == "builtin:java"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == [validate_command]


@pytest.mark.unit
def test_auto_detection_prefers_docker_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "docker-compose"
    assert result.profile.docker.mode == DockerMode.dind


@pytest.mark.unit
def test_auto_detection_keeps_docker_compose_ahead_of_language_profiles(
    tmp_path: Path,
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "docker-compose"


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
def test_auto_detection_keeps_nextjs_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "latest"}}),
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "nextjs"


@pytest.mark.unit
def test_auto_detection_keeps_node_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "node"


@pytest.mark.unit
def test_auto_detection_keeps_python_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    (tmp_path / "CMakeLists.txt").write_text("project(app)\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "python"


@pytest.mark.unit
def test_auto_detection_keeps_aira_ahead_of_language_profiles(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "aira-agent"\n',
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "aira"


@pytest.mark.unit
def test_auto_detection_detects_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "go"
    assert result.profile.source == "detector:go"
    assert result.profile.phases.setup[0].command == "go mod download"
    assert [c.command for c in result.profile.phases.validate_commands] == ["go test ./..."]


@pytest.mark.unit
def test_auto_detection_detects_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "rust"
    assert result.profile.source == "detector:rust"
    assert result.profile.phases.setup[0].command == "cargo fetch"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "cargo test --all-targets"
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker", "contents", "setup_command", "validate_command"),
    [
        (
            "mvnw",
            "#!/bin/sh\n",
            "./mvnw -B -DskipTests dependency:go-offline",
            "./mvnw -B test",
        ),
        (
            "pom.xml",
            "<project></project>\n",
            "mvn -B -DskipTests dependency:go-offline",
            "mvn -B test",
        ),
        (
            "build.gradle",
            "plugins { id 'java' }\n",
            "gradle --no-daemon dependencies",
            "gradle --no-daemon test",
        ),
        (
            "gradlew",
            "#!/bin/sh\n",
            "./gradlew --no-daemon dependencies",
            "./gradlew --no-daemon test",
        ),
    ],
)
def test_auto_detection_detects_java(
    tmp_path: Path,
    marker: str,
    contents: str,
    setup_command: str,
    validate_command: str,
) -> None:
    (tmp_path / marker).write_text(contents, encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == setup_command
    assert [c.command for c in result.profile.phases.validate_commands] == [validate_command]


@pytest.mark.unit
def test_auto_detection_prefers_maven_wrapper_over_plain_maven(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == "./mvnw -B -DskipTests dependency:go-offline"
    assert [c.command for c in result.profile.phases.validate_commands] == ["./mvnw -B test"]


@pytest.mark.unit
def test_auto_detection_prefers_gradle_wrapper_over_plain_maven(tmp_path: Path) -> None:
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "java"
    assert result.profile.source == "detector:java"
    assert result.profile.phases.setup[0].command == "./gradlew --no-daemon dependencies"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "./gradlew --no-daemon test"
    ]


@pytest.mark.unit
def test_auto_detection_detects_cpp(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(app)\n", encoding="utf-8")
    result = ProfileResolver().resolve(worktree_path=tmp_path, profile_ref="auto")
    assert result.profile.name == "cpp"
    assert result.profile.source == "detector:cpp"
    assert result.profile.phases.setup[0].command == "cmake -S . -B build"
    assert [c.command for c in result.profile.phases.validate_commands] == [
        "cmake --build build",
        "ctest --test-dir build --output-on-failure",
    ]


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
