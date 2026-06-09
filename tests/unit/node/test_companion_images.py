"""Tests for companion image pre-build caching (issue #298)."""

from __future__ import annotations

import asyncio

import pytest

from awf.node.companion_images import (
    CompanionImageBuilder,
    companion_image_prune_command,
    companion_image_tag,
)
from awf.node.compose_manager import (
    COMPANION_IMAGE_MANAGED_LABEL,
    COMPANION_IMAGE_NAME_LABEL,
    ComposeOperationError,
)


class _FakeCompose:
    """Minimal ComposeManager stand-in exposing only the builder's public calls."""

    def __init__(
        self,
        *,
        exists: bool = False,
        inspect_error: ComposeOperationError | None = None,
        build_error: Exception | None = None,
        build_gate: asyncio.Event | None = None,
    ) -> None:
        self.exists_result = exists
        self.inspect_error = inspect_error
        self.build_error = build_error
        self.build_gate = build_gate
        self.exists_calls: list[str] = []
        self.inspect_calls: list[list[str]] = []
        self.build_calls: list[dict[str, object]] = []
        self._built_tags: set[str] = set()

    async def companion_image_exists(self, tag: str) -> bool:
        await asyncio.sleep(0)  # yield so concurrent callers can interleave
        self.exists_calls.append(tag)
        return self.exists_result or tag in self._built_tags

    async def build_companion_image(
        self,
        *,
        tag: str,
        build_context: str,
        dockerfile: str,
        labels: dict[str, str] | None = None,
        capture_timeout_seconds: float | None = None,
    ) -> None:
        # An optional gate lets a test hold the build in flight (so a waiter can
        # be cancelled while the shared build is still running) and only record
        # the call once released -- a build cancelled mid-flight never appends.
        if self.build_gate is not None:
            await self.build_gate.wait()
        await asyncio.sleep(0)
        self.build_calls.append(
            {
                "tag": tag,
                "build_context": build_context,
                "dockerfile": dockerfile,
                "labels": dict(labels or {}),
                "capture_timeout_seconds": capture_timeout_seconds,
            }
        )
        if self.build_error is not None:
            raise self.build_error
        self._built_tags.add(tag)  # a successful build makes that exact tag present

    async def companion_image_inspect(self, tag: str) -> bool:
        """Record inspect probes and return whether the fake tag is present."""
        self.inspect_calls.append(["image", "inspect", tag])
        if self.inspect_error is not None:
            detail = f"{self.inspect_error.stderr}\n{self.inspect_error.stdout}".lower()
            if "no such image" in detail:
                return False
            raise self.inspect_error
        return self.exists_result or tag in self._built_tags


@pytest.mark.unit
def test_companion_image_tag_sanitizes_name_and_truncates_sha() -> None:
    """Tag generation sanitizes the companion name and truncates the commit SHA."""
    tag = companion_image_tag(
        "Aira Agent/Backend",
        "ABCDEF0123456789deadbeef",
        build_context=".",
        dockerfile="Dockerfile",
    )
    # The repo name and sha are preserved; a build-inputs digest is appended so
    # differing build definitions never share a tag (see the collision test).
    repo, _, image_tag = tag.partition(":")
    assert repo == "awf-companion-aira-agent-backend"
    sha, _, digest = image_tag.partition("-")
    assert sha == "abcdef012345"
    assert digest and all(char in "0123456789abcdef" for char in digest)


@pytest.mark.unit
def test_companion_image_tag_falls_back_when_name_sanitizes_empty() -> None:
    """Tag generation falls back to a default when the name sanitizes to empty."""
    tag = companion_image_tag("///", "abc123", build_context=".", dockerfile="Dockerfile")
    assert tag.startswith("awf-companion-companion:")


@pytest.mark.unit
def test_companion_image_tag_varies_with_build_inputs() -> None:
    # Regression for PRRT_kwDOSJAM6s6F5072: the cache key must fold in the build
    # inputs. Two companions sharing a name and commit but differing in
    # build_context or dockerfile must resolve to distinct tags so the cache
    # never serves an image built from a different build definition; identical
    # build inputs must still resolve to the same tag so cross-workspace reuse
    # (issue #298) keeps working.
    """The tag varies when the build context or Dockerfile inputs differ."""
    base = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    same = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    other_context = companion_image_tag(
        "backend", "abc123def456", build_context="services/api", dockerfile="Dockerfile"
    )
    other_dockerfile = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile.dev"
    )

    assert base == same
    assert base != other_context
    assert base != other_dockerfile
    assert other_context != other_dockerfile


@pytest.mark.unit
def test_companion_image_prune_command_scopes_to_managed_label_and_age() -> None:
    """The prune command is scoped to the managed label and retention age."""
    command = companion_image_prune_command(72)
    assert command[:4] == ["docker", "image", "prune", "--all"]
    assert "--force" in command
    assert f"label={COMPANION_IMAGE_MANAGED_LABEL}=true" in command
    assert "until=72h" in command


@pytest.mark.unit
async def test_ensure_reuses_existing_image_without_building() -> None:
    """ensure() reuses an existing tagged image without rebuilding."""
    compose = _FakeCompose(exists=True)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    tag = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert tag == companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    assert compose.build_calls == []


@pytest.mark.unit
async def test_companion_image_exists_returns_true_when_tag_present() -> None:
    """The launch-time existence helper returns True for a present image."""
    compose = _FakeCompose(exists=True)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    assert await builder.companion_image_exists("awf-companion-backend:abc") is True
    assert compose.inspect_calls == [["image", "inspect", "awf-companion-backend:abc"]]


@pytest.mark.unit
async def test_companion_image_exists_returns_false_when_tag_missing() -> None:
    """The launch-time existence helper returns False for a confirmed missing image."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    assert await builder.companion_image_exists("missing:tag") is False
    assert compose.inspect_calls == [["image", "inspect", "missing:tag"]]


@pytest.mark.unit
async def test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing() -> None:
    """Docker inspect may classify missing-image stderr as daemon unavailable."""
    probe_error = ComposeOperationError(
        operation="image inspect",
        returncode=1,
        stdout="",
        stderr="Error response from daemon: No such image: awf-companion-backend:abc",
        reason_code="DOCKER_UNAVAILABLE",
    )
    compose = _FakeCompose(exists=False, inspect_error=probe_error)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    assert await builder.companion_image_exists("awf-companion-backend:abc") is False
    assert compose.inspect_calls == [["image", "inspect", "awf-companion-backend:abc"]]


@pytest.mark.unit
async def test_companion_image_exists_preserves_probe_error_reason_code() -> None:
    """The launch-time existence helper does not convert Docker errors to missing."""
    probe_error = ComposeOperationError(
        operation="image inspect",
        returncode=1,
        stdout="",
        stderr="Cannot connect to the Docker daemon",
        reason_code="DOCKER_UNAVAILABLE",
    )
    compose = _FakeCompose(exists=False, inspect_error=probe_error)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    with pytest.raises(ComposeOperationError) as raised:
        await builder.companion_image_exists("awf-companion-backend:abc")

    assert raised.value is probe_error
    assert raised.value.reason_code == "DOCKER_UNAVAILABLE"


@pytest.mark.unit
async def test_companion_image_exists_preserves_unexpected_inspect_failure() -> None:
    """Only confirmed missing-image inspect failures become False."""
    probe_error = ComposeOperationError(
        operation="image inspect",
        returncode=1,
        stdout="",
        stderr="permission denied",
        reason_code="COMPOSE_COMMAND_FAILED",
    )
    compose = _FakeCompose(exists=False, inspect_error=probe_error)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    with pytest.raises(ComposeOperationError) as raised:
        await builder.companion_image_exists("awf-companion-backend:abc")

    assert raised.value is probe_error


@pytest.mark.unit
async def test_ensure_builds_with_managed_labels_when_missing() -> None:
    """ensure() builds the image with managed labels when it is missing."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    tag = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="sub/Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert tag == companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="sub/Dockerfile"
    )
    assert len(compose.build_calls) == 1
    call = compose.build_calls[0]
    assert call["build_context"] == "/ctx"
    assert call["dockerfile"] == "sub/Dockerfile"
    assert call["labels"] == {
        COMPANION_IMAGE_MANAGED_LABEL: "true",
        COMPANION_IMAGE_NAME_LABEL: "backend",
    }


@pytest.mark.unit
async def test_ensure_skips_caching_without_commit_sha() -> None:
    """ensure() skips caching and returns None when no commit SHA is available."""
    compose = _FakeCompose()
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    result = await builder.ensure(
        name="backend",
        commit_sha="   ",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert result is None
    assert compose.exists_calls == []
    assert compose.build_calls == []


@pytest.mark.unit
async def test_ensure_falls_back_to_none_on_build_failure() -> None:
    """ensure() returns None to fall back to an inline build on build failure."""
    compose = _FakeCompose(
        exists=False,
        build_error=ComposeOperationError(
            operation="build",
            returncode=1,
            stdout="",
            stderr="boom",
        ),
    )
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    result = await builder.ensure(
        name="backend",
        commit_sha="abc123",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert result is None
    assert len(compose.build_calls) == 1


@pytest.mark.unit
async def test_ensure_deduplicates_concurrent_builds_for_same_tag() -> None:
    """Concurrent ensure() calls for the same tag share a single build."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    results = await asyncio.gather(
        *(
            builder.ensure(
                name="backend",
                commit_sha="abc123def456",
                build_context="/ctx",
                dockerfile="Dockerfile",
                relative_build_context=".",
                capture_timeout_seconds=660.0,
            )
            for _ in range(4)
        )
    )

    expected = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    assert results == [expected, expected, expected, expected]
    assert len(compose.build_calls) == 1  # the per-tag lock collapses the wave


@pytest.mark.unit
async def test_ensure_does_not_bind_larger_budget_joiner_to_shorter_cap() -> None:
    # Regression for PRRT_kwDOSJAM6s6F6dsw: concurrent dispatches that share a
    # companion tag but carry different effective compose-up budgets must each
    # build under their own capture_timeout_seconds. Keying the in-flight registry
    # by tag alone made a slower stack join (and inherit the shorter cap of) the
    # faster stack's build, timing the cache pre-build out earlier than that
    # stack's own inline `docker compose up` build would -- the invariant the
    # per-stack budget is meant to guarantee. Dedup is keyed by (tag, budget).
    """A larger-budget caller must not inherit a shorter in-flight build cap."""
    gate = asyncio.Event()
    compose = _FakeCompose(exists=False, build_gate=gate)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    def _ensure(timeout: float) -> asyncio.Task[str | None]:
        return asyncio.create_task(
            builder.ensure(
                name="backend",
                commit_sha="abc123def456",
                build_context="/ctx",
                dockerfile="Dockerfile",
                relative_build_context=".",
                capture_timeout_seconds=timeout,
            )
        )

    short = _ensure(60.0)
    long = _ensure(600.0)
    # Let both dispatches pass the existence check and block on the gate before
    # releasing, so neither short-circuits on the other's cache write. Bounded so
    # the buggy single-build path proceeds (and fails the cap assertion) instead
    # of hanging.
    for _ in range(100):
        if len(compose.exists_calls) >= 2:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(short, long)

    expected = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    assert results == [expected, expected]
    # Each stack built under its own budget rather than sharing the first cap.
    assert sorted(call["capture_timeout_seconds"] for call in compose.build_calls) == [60.0, 600.0]


@pytest.mark.unit
async def test_ensure_forwards_capture_timeout_to_build() -> None:
    # Regression for PRRT_kwDOSJAM6s6F504S: the pre-build must honor the caller's
    # compose-up build budget rather than the fixed 1800s default, so the cache
    # path can never time out earlier than the inline `docker compose up` build.
    """ensure() forwards the configured capture timeout to the build call."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=1860.0,
    )

    assert compose.build_calls[0]["capture_timeout_seconds"] == 1860.0


@pytest.mark.unit
async def test_ensure_shares_one_build_across_a_failing_concurrent_wave() -> None:
    # Regression for PRRT_kwDOSJAM6s6F506l: a failing build must be attempted
    # once per concurrent wave and propagated to every waiter, not retried
    # sequentially once per waiter -- a broken companion would otherwise block
    # the worker queue for N * timeout.
    """A failing concurrent wave shares the single in-flight build task."""
    compose = _FakeCompose(
        exists=False,
        build_error=ComposeOperationError(
            operation="build",
            returncode=1,
            stdout="",
            stderr="boom",
        ),
    )
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    results = await asyncio.gather(
        *(
            builder.ensure(
                name="backend",
                commit_sha="abc123def456",
                build_context="/ctx",
                dockerfile="Dockerfile",
                relative_build_context=".",
                capture_timeout_seconds=660.0,
            )
            for _ in range(4)
        )
    )

    assert results == [None, None, None, None]
    assert len(compose.build_calls) == 1  # the wave shares one failing build


@pytest.mark.unit
async def test_ensure_retries_build_after_a_failed_wave() -> None:
    # Regression for PRRT_kwDOSJAM6s6F506l: a failed build is dropped from the
    # in-flight registry so a later dispatch retries it instead of replaying a
    # cached failure forever.
    """ensure() retries the build on a later call after a failed wave."""
    compose = _FakeCompose(
        exists=False,
        build_error=ComposeOperationError(
            operation="build",
            returncode=1,
            stdout="",
            stderr="boom",
        ),
    )
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    first = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )
    second = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert first is None
    assert second is None
    assert len(compose.build_calls) == 2  # the second wave retried the build


@pytest.mark.unit
async def test_ensure_does_not_retain_finished_build_tasks() -> None:
    # Regression for PRRT_kwDOSJAM6s6F506l: the in-flight registry must not grow
    # without bound -- a finished build (here a success) is dropped so only
    # in-flight builds are ever held.
    """Finished build tasks are dropped from the per-tag registry."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ctx",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )
    await asyncio.sleep(0)  # let the done-callback drop the finished task

    assert builder._builds == {}  # noqa: SLF001


@pytest.mark.unit
async def test_ensure_does_not_reuse_image_across_differing_build_inputs() -> None:
    # Regression for PRRT_kwDOSJAM6s6F5072: two workspaces requesting the same
    # companion name at the same commit but with different build inputs must not
    # share a tag. Otherwise the second launch would see the first build's tag
    # via companion_image_exists() and run the wrong image. The resolved
    # build_context is a per-worktree absolute path, so the cache key keys on the
    # repo-relative build inputs (relative_build_context + dockerfile) instead.
    """ensure() does not reuse an image when the build inputs differ."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    first = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ws-a/worktree",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )
    second = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ws-b/worktree",
        dockerfile="Dockerfile.dev",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert first is not None and second is not None
    assert first != second  # distinct tags despite identical name + commit
    assert len(compose.build_calls) == 2  # the second built its own image


@pytest.mark.unit
async def test_ensure_reuses_image_across_workspaces_for_identical_build_inputs() -> None:
    # The cross-workspace cache (issue #298) must still hit: identical
    # repo-relative build inputs at the same commit resolve to the same tag even
    # though each workspace resolves build_context to a different absolute path.
    """ensure() reuses the image across workspaces for identical build inputs."""
    compose = _FakeCompose(exists=False)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    first = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ws-a/worktree",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )
    second = await builder.ensure(
        name="backend",
        commit_sha="abc123def456",
        build_context="/ws-b/worktree",
        dockerfile="Dockerfile",
        relative_build_context=".",
        capture_timeout_seconds=660.0,
    )

    assert first == second  # same tag -> reused across workspaces
    assert len(compose.build_calls) == 1  # the second reused the first build


@pytest.mark.unit
async def test_ensure_waiter_cancellation_does_not_abort_shared_build() -> None:
    # Regression for PRRT_kwDOSJAM6s6F6Lh0: concurrent waiters for the same tag
    # share one in-flight build task. Awaiting that Task directly would propagate
    # a single waiter's cancellation into the shared Task (a waiter awaiting a
    # Task becomes that Task's _fut_waiter, so cancelling the waiter cancels the
    # Task), aborting the build for *every* other waiter in the wave. asyncio
    # .shield isolates the shared build so a cancelled waiter only abandons its
    # own wait while the build keeps running for the survivors.
    """Cancelling one waiter leaves the shared build intact for the others."""
    gate = asyncio.Event()
    compose = _FakeCompose(exists=False, build_gate=gate)
    builder = CompanionImageBuilder(compose)  # type: ignore[arg-type]

    async def _ensure() -> str | None:
        return await builder.ensure(
            name="backend",
            commit_sha="abc123def456",
            build_context="/ctx",
            dockerfile="Dockerfile",
            relative_build_context=".",
            capture_timeout_seconds=660.0,
        )

    cancelled_waiter = asyncio.create_task(_ensure())
    surviving_waiter = asyncio.create_task(_ensure())
    # Let both waiters attach to the one shared build task before cancelling.
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(builder._builds) == 1  # noqa: SLF001  # a single shared build

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    # Releasing the gate lets the shared build finish; the surviving waiter must
    # still receive the tag rather than a propagated CancelledError.
    gate.set()
    result = await surviving_waiter

    expected = companion_image_tag(
        "backend", "abc123def456", build_context=".", dockerfile="Dockerfile"
    )
    assert result == expected
    assert len(compose.build_calls) == 1  # the shared build ran once, uncancelled
