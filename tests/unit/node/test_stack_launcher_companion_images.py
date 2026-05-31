"""Companion image pre-build wiring in the stack launcher (issue #298)."""

from __future__ import annotations

import asyncio
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
async def test_companion_prebuilds_run_concurrently(tmp_path: Path) -> None:
    # Regression for PRRT_kwDOSJAM6s6F506n: independent companion pre-builds are
    # dispatched concurrently, so a multi-companion workspace's provisioning
    # latency is the slowest single build rather than the sum of all builds. A
    # sequential loop would never let the second `ensure` start before the first
    # returns, deadlocking the barrier below and tripping the wait_for timeout.
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
            capture_timeout_seconds: float,
        ) -> str | None:
            nonlocal entered
            del commit_sha, build_context, dockerfile, capture_timeout_seconds
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
    launcher = _launcher(None)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
