"""Companion image pre-build wiring in the stack launcher (issue #298)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher


class _StubCompose:
    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        del spec, wait
        raise AssertionError("up should not be called by these tests")


class _RecordingBuilder:
    def __init__(self, *, tag: str | None) -> None:
        self.tag = tag
        self.calls: list[dict[str, object]] = []

    async def ensure(
        self,
        *,
        name: str,
        commit_sha: str,
        build_context: str,
        dockerfile: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        self.calls.append(
            {
                "name": name,
                "commit_sha": commit_sha,
                "build_context": build_context,
                "dockerfile": dockerfile,
                "capture_timeout_seconds": capture_timeout_seconds,
            }
        )
        return self.tag


def _materialized(root: Path, *, commit_sha: str = "abc123def456") -> MaterializedCompanionService:
    root.mkdir(parents=True, exist_ok=True)
    return MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(name="backend", repo_url="git@example.com:backend.git"),
        layout=WorktreeLayout(
            mirror_path=root.parent / "mirror.git",
            worktree_path=root,
            branch_name="awf/companion",
        ),
        commit_sha=commit_sha,
    )


def _launcher(builder: _RecordingBuilder | None) -> ComposeStackLauncher:
    return ComposeStackLauncher(
        compose=_StubCompose(),  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )


@pytest.mark.unit
async def test_prebuilt_tag_is_applied_as_image(tmp_path: Path) -> None:
    builder = _RecordingBuilder(tag="awf-companion-backend:abc123def456")
    launcher = _launcher(builder)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image == "awf-companion-backend:abc123def456"
    assert builder.calls[0]["name"] == "backend"
    assert builder.calls[0]["commit_sha"] == "abc123def456"
    assert builder.calls[0]["build_context"] == str((tmp_path / "backend").resolve())
    assert builder.calls[0]["dockerfile"] == "Dockerfile"
    assert builder.calls[0]["capture_timeout_seconds"] == 660.0


@pytest.mark.unit
async def test_failed_prebuild_falls_back_to_build(tmp_path: Path) -> None:
    builder = _RecordingBuilder(tag=None)
    launcher = _launcher(builder)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
    assert builder.calls  # builder was consulted


@pytest.mark.unit
async def test_no_builder_leaves_companion_as_build(tmp_path: Path) -> None:
    launcher = _launcher(None)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
