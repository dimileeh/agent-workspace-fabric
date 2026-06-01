"""Companion image pre-build wiring in the stack launcher (issue #298)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.models import WorkspaceProfile


class _StubCompose:
    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        del spec, wait
        raise AssertionError("up should not be called by these tests")


class _RecordingCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        self.specs.append(spec)
        self.waits.append(wait)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_launcher"),
            compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
        )


class _RecordingBuilder:
    def __init__(self, *, tag: str | None, exists: bool = True) -> None:
        self.tag = tag
        self.exists = exists
        self.calls: list[dict[str, object]] = []
        self.exists_calls: list[str] = []

    async def ensure(
        self,
        *,
        name: str,
        commit_sha: str,
        build_context: str,
        dockerfile: str,
        relative_build_context: str,
        capture_timeout_seconds: float,
    ) -> str | None:
        self.calls.append(
            {
                "name": name,
                "commit_sha": commit_sha,
                "build_context": build_context,
                "dockerfile": dockerfile,
                "relative_build_context": relative_build_context,
                "capture_timeout_seconds": capture_timeout_seconds,
            }
        )
        return self.tag

    async def companion_image_exists(self, tag: str) -> bool:
        self.exists_calls.append(tag)
        return self.exists


def _materialized(
    root: Path, *, name: str = "backend", commit_sha: str = "abc123def456"
) -> MaterializedCompanionService:
    root.mkdir(parents=True, exist_ok=True)
    return MaterializedCompanionService(
        spec=WorkspaceCompanionSpec(name=name, repo_url=f"git@example.com:{name}.git"),
        layout=WorktreeLayout(
            mirror_path=root.parent / f"{name}-mirror.git",
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


def _launch_request(
    root: Path,
    *,
    companion_name: str = "backend",
) -> WorkspaceStackLaunchRequest:
    return WorkspaceStackLaunchRequest(
        workspace_id="ws_launcher",
        layout=WorktreeLayout(
            mirror_path=root / "repo.git",
            worktree_path=root / "repo",
            branch_name="awf/ws_launcher",
        ),
        profile=WorkspaceProfile(name="generic"),
        companions=(_materialized(root / companion_name, name=companion_name),),
        companion_graph_prevalidated=True,
    )


@pytest.mark.unit
async def test_prebuilt_tag_is_applied_as_image(tmp_path: Path) -> None:
    """A successful pre-build applies the resulting tag as the service image."""
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
    # The repo-relative spec build context drives the cache key (the absolute
    # build_context above is per-worktree and would break cross-workspace reuse).
    assert builder.calls[0]["relative_build_context"] == "."
    assert builder.calls[0]["capture_timeout_seconds"] == 660.0


@pytest.mark.unit
async def test_failed_prebuild_falls_back_to_build(tmp_path: Path) -> None:
    """A failed pre-build leaves the companion using an inline build:."""
    builder = _RecordingBuilder(tag=None)
    launcher = _launcher(builder)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
    assert builder.calls  # builder was consulted


@pytest.mark.unit
async def test_launch_keeps_prebuilt_companion_image_when_revalidation_succeeds(
    tmp_path: Path,
) -> None:
    """Launch-time revalidation keeps a cache-hit image that still exists."""
    compose = _RecordingCompose()
    builder = _RecordingBuilder(tag="awf-companion-backend:abc123def456", exists=True)
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )

    await launcher.launch(_launch_request(tmp_path))

    companion = compose.specs[0].companions[0]
    assert companion.image == "awf-companion-backend:abc123def456"
    assert companion.build_context == str((tmp_path / "backend").resolve())
    assert companion.dockerfile == "Dockerfile"
    assert builder.exists_calls == ["awf-companion-backend:abc123def456"]


@pytest.mark.unit
async def test_launch_revalidates_prebuilt_companion_image_and_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    """A pruned cache-hit image falls back to inline build before compose up."""
    compose = _RecordingCompose()
    builder = _RecordingBuilder(tag="awf-companion-backend:abc123def456", exists=False)
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )

    await launcher.launch(_launch_request(tmp_path))

    companion = compose.specs[0].companions[0]
    assert companion.image is None
    assert companion.build_context == str((tmp_path / "backend").resolve())
    assert companion.dockerfile == "Dockerfile"
    assert builder.exists_calls == ["awf-companion-backend:abc123def456"]
    assert compose.waits == [True]


@pytest.mark.unit
async def test_companion_prebuilds_run_concurrently(tmp_path: Path) -> None:
    # Regression for PRRT_kwDOSJAM6s6F506n: independent companion pre-builds are
    # dispatched concurrently, so a multi-companion workspace's provisioning
    # latency is the slowest single build rather than the sum of all builds. A
    # sequential loop would never let the second `ensure` start before the first
    # returns, deadlocking the barrier below and tripping the wait_for timeout.
    """Multiple companion pre-builds run concurrently."""
    both_entered = asyncio.Event()
    entered = 0

    class _BarrierBuilder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def ensure(
            self,
            *,
            name: str,
            commit_sha: str,
            build_context: str,
            dockerfile: str,
            relative_build_context: str,
            capture_timeout_seconds: float,
        ) -> str | None:
            nonlocal entered
            del commit_sha, build_context, dockerfile, relative_build_context
            del capture_timeout_seconds
            self.calls.append(name)
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            return None

    builder = _BarrierBuilder()
    launcher = _launcher(builder)  # type: ignore[arg-type]
    backend = _materialized(tmp_path / "backend", name="backend")
    frontend = _materialized(tmp_path / "frontend", name="frontend")

    services = await asyncio.wait_for(
        launcher._build_companion_services(  # noqa: SLF001
            (backend, frontend), capture_timeout_seconds=660.0
        ),
        timeout=1.0,
    )

    assert both_entered.is_set()
    assert [service.name for service in services] == ["backend", "frontend"]
    assert sorted(builder.calls) == ["backend", "frontend"]


@pytest.mark.unit
async def test_no_builder_leaves_companion_as_build(tmp_path: Path) -> None:
    """Without a builder the companion is left using an inline build:."""
    launcher = _launcher(None)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
