"""Companion image pre-build wiring in the stack launcher (issue #298)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.node.companion_services import MaterializedCompanionService, WorkspaceCompanionSpec
from awf.node.compose_manager import (
    CompanionService,
    ComposeOperationError,
    ComposeProjectPaths,
    WorkspaceComposeSpec,
)
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import (
    ComposeStackLauncher,
    WorkspaceStackLaunchRequest,
    _compose_up_reports_missing_image,
)
from awf.profiles.models import WorkspaceProfile


class _StubCompose:
    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        del spec, wait
        raise AssertionError("up should not be called by these tests")


class _RecordingCompose:
    """Compose double that records launched specs."""

    def __init__(self) -> None:
        """Initialize captured compose-up inputs."""
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        """Record the rendered spec and return deterministic compose paths."""
        self.specs.append(spec)
        self.waits.append(wait)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_launcher"),
            compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
        )


class _MissingImageOnceCompose:
    """Compose double that loses a pre-built companion tag on the first launch."""

    def __init__(
        self,
        *,
        missing_tag: str,
        reason_code: str = "COMPOSE_COMMAND_FAILED",
    ) -> None:
        """Initialize the missing-image failure and recorded launch specs."""
        self.missing_tag = missing_tag
        self.reason_code = reason_code
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        """Fail once with Docker's missing-image text, then succeed."""
        self.specs.append(spec)
        self.waits.append(wait)
        if len(self.specs) == 1:
            raise ComposeOperationError(
                operation="up",
                returncode=1,
                stdout="",
                stderr=f"Error response from daemon: No such image: {self.missing_tag}",
                reason_code=self.reason_code,
            )
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_launcher"),
            compose_file=Path("/tmp/awf-compose/ws_launcher/compose.yml"),
        )


class _RecordingBuilder:
    def __init__(self, *, tag: str | None, exists: bool = True) -> None:
        """Initialize the fake pre-build result and existence state."""
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
        """Record launch-time image probes and return the configured result."""
        self.exists_calls.append(tag)
        return self.exists


def _compose_error(stderr: str) -> ComposeOperationError:
    """Build a compose-up error for missing-image classifier tests."""
    return ComposeOperationError(
        operation="up",
        returncode=1,
        stdout="",
        stderr=stderr,
        reason_code="COMPOSE_COMMAND_FAILED",
    )


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
    """Build a launch request with one materialized companion."""
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
async def test_launch_retries_with_inline_build_when_prebuilt_image_pruned_after_revalidation(
    tmp_path: Path,
) -> None:
    """A tag pruned after revalidation is cleared and retried as an inline build."""
    tag = "awf-companion-backend:abc123def456"
    compose = _MissingImageOnceCompose(missing_tag=tag)
    builder = _RecordingBuilder(tag=tag, exists=True)
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )

    await launcher.launch(_launch_request(tmp_path))

    first_companion = compose.specs[0].companions[0]
    retry_companion = compose.specs[1].companions[0]
    assert first_companion.image == tag
    assert retry_companion.image is None
    assert retry_companion.build_context == str((tmp_path / "backend").resolve())
    assert retry_companion.dockerfile == "Dockerfile"
    assert builder.exists_calls == [tag]
    assert compose.waits == [True, True]


@pytest.mark.unit
async def test_launch_revalidates_remaining_prebuilt_companion_images_before_retry(
    tmp_path: Path,
) -> None:
    """Retry-time revalidation clears cache-hit tags not named in the first failure."""
    backend_tag = "awf-companion-backend:abc123def456"
    worker_tag = "awf-companion-worker:abc123def456"

    class _RetryRevalidationBuilder:
        """Builder double that prunes the worker tag after first launch validation."""

        def __init__(self) -> None:
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
            del commit_sha, build_context, dockerfile, relative_build_context
            del capture_timeout_seconds
            return {"backend": backend_tag, "worker": worker_tag}[name]

        async def companion_image_exists(self, tag: str) -> bool:
            previous_calls = self.exists_calls.count(tag)
            self.exists_calls.append(tag)
            return tag != worker_tag or previous_calls == 0

    compose = _MissingImageOnceCompose(missing_tag=backend_tag)
    builder = _RetryRevalidationBuilder()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )
    request = _launch_request(tmp_path)
    request = WorkspaceStackLaunchRequest(
        workspace_id=request.workspace_id,
        layout=request.layout,
        profile=request.profile,
        companions=(
            _materialized(tmp_path / "backend", name="backend"),
            _materialized(tmp_path / "worker", name="worker"),
        ),
        companion_graph_prevalidated=True,
    )

    await launcher.launch(request)

    first_companions = compose.specs[0].companions
    retry_companions = compose.specs[1].companions
    assert [companion.image for companion in first_companions] == [backend_tag, worker_tag]
    assert [companion.image for companion in retry_companions] == [None, None]
    assert builder.exists_calls.count(backend_tag) == 1
    assert builder.exists_calls.count(worker_tag) == 2
    assert compose.waits == [True, True]


@pytest.mark.unit
async def test_launch_retries_daemon_classified_missing_prebuilt_companion_image(
    tmp_path: Path,
) -> None:
    """A daemon-classified missing companion tag still retries with an inline build."""
    tag = "awf-companion-backend:abc123def456"
    compose = _MissingImageOnceCompose(missing_tag=tag, reason_code="DOCKER_UNAVAILABLE")
    builder = _RecordingBuilder(tag=tag, exists=True)
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )

    await launcher.launch(_launch_request(tmp_path))

    first_companion = compose.specs[0].companions[0]
    retry_companion = compose.specs[1].companions[0]
    assert first_companion.image == tag
    assert retry_companion.image is None
    assert retry_companion.build_context == str((tmp_path / "backend").resolve())
    assert retry_companion.dockerfile == "Dockerfile"
    assert builder.exists_calls == [tag]
    assert compose.waits == [True, True]


@pytest.mark.unit
async def test_launch_does_not_retry_missing_non_companion_image(tmp_path: Path) -> None:
    """Missing-image errors must name a pre-built companion tag to trigger retry."""
    compose = _MissingImageOnceCompose(missing_tag="postgres:16")
    builder = _RecordingBuilder(tag="awf-companion-backend:abc123def456", exists=True)
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )

    with pytest.raises(ComposeOperationError):
        await launcher.launch(_launch_request(tmp_path))

    assert len(compose.specs) == 1
    assert compose.specs[0].companions[0].image == "awf-companion-backend:abc123def456"
    assert compose.waits == [True]


@pytest.mark.unit
def test_missing_image_detector_rejects_plain_not_found_near_companion_tag() -> None:
    """Plain not-found text near the tag does not confirm a missing image."""
    tag = "awf-companion-backend:abc123def456"

    assert not _compose_up_reports_missing_image(
        _compose_error(f"pulling {tag}: image not found"),
        tag,
    )


@pytest.mark.unit
def test_missing_image_detector_accepts_no_such_image_for_companion_tag() -> None:
    """Docker's no-such-image message confirms the companion tag is absent."""
    tag = "awf-companion-backend:abc123def456"

    assert _compose_up_reports_missing_image(
        _compose_error(f"No such image: {tag}"),
        tag,
    )


@pytest.mark.unit
def test_missing_image_detector_rejects_unrelated_not_found_when_tag_is_elsewhere() -> None:
    """Unrelated not-found text must not retry just because the tag is mentioned."""
    tag = "awf-companion-backend:abc123def456"

    assert not _compose_up_reports_missing_image(
        _compose_error(
            f"preparing service for {tag}; "
            "configuration lookup failed because /tmp/workspace/docker-compose.override.yml "
            "was not found"
        ),
        tag,
    )


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
async def test_companion_image_revalidation_runs_concurrently(tmp_path: Path) -> None:
    """Multiple launch-time companion image probes run concurrently."""
    both_entered = asyncio.Event()
    entered = 0

    class _BarrierBuilder:
        """Builder double that blocks until both revalidation probes enter."""

        def __init__(self) -> None:
            """Initialize recorded launch-time probe tags."""
            self.calls: list[str] = []

        async def companion_image_exists(self, tag: str) -> bool:
            """Block each probe until its peer has also reached the barrier."""
            nonlocal entered
            self.calls.append(tag)
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            return tag != "missing:tag"

    builder = _BarrierBuilder()
    launcher = ComposeStackLauncher(
        compose=_StubCompose(),  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:latest",
        companion_image_builder=builder,  # type: ignore[arg-type]
    )
    spec = WorkspaceComposeSpec(
        workspace_id="ws_launcher",
        worktree_host_path=tmp_path,
        companions=(
            CompanionService(
                name="backend",
                build_context=str(tmp_path / "backend"),
                image="keep:tag",
            ),
            CompanionService(
                name="worker",
                build_context=str(tmp_path / "worker"),
                image="missing:tag",
            ),
            CompanionService(
                name="source",
                build_context=str(tmp_path / "source"),
                image=None,
            ),
        ),
    )

    revalidated = await asyncio.wait_for(
        launcher._revalidate_prebuilt_companion_images(spec),  # noqa: SLF001
        timeout=1.0,
    )

    assert both_entered.is_set()
    assert sorted(builder.calls) == ["keep:tag", "missing:tag"]
    assert revalidated.companions[0].image == "keep:tag"
    assert revalidated.companions[1].image is None
    assert revalidated.companions[2] == spec.companions[2]


@pytest.mark.unit
async def test_no_builder_leaves_companion_as_build(tmp_path: Path) -> None:
    """Without a builder the companion is left using an inline build:."""
    launcher = _launcher(None)
    materialized = _materialized(tmp_path / "backend")

    services = await launcher._build_companion_services(  # noqa: SLF001
        (materialized,), capture_timeout_seconds=660.0
    )

    assert services[0].image is None
