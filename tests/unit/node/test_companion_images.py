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
    """Minimal ComposeManager stand-in exposing only the builder's docker calls."""

    def __init__(self, *, exists: bool = False, build_error: Exception | None = None) -> None:
        self.exists_result = exists
        self.build_error = build_error
        self.exists_calls: list[str] = []
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


@pytest.mark.unit
def test_companion_image_tag_sanitizes_name_and_truncates_sha() -> None:
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
    command = companion_image_prune_command(72)
    assert command[:4] == ["docker", "image", "prune", "--all"]
    assert "--force" in command
    assert f"label={COMPANION_IMAGE_MANAGED_LABEL}=true" in command
    assert "until=72h" in command


@pytest.mark.unit
async def test_ensure_reuses_existing_image_without_building() -> None:
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
async def test_ensure_builds_with_managed_labels_when_missing() -> None:
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
async def test_ensure_forwards_capture_timeout_to_build() -> None:
    # Regression for PRRT_kwDOSJAM6s6F504S: the pre-build must honor the caller's
    # compose-up build budget rather than the fixed 1800s default, so the cache
    # path can never time out earlier than the inline `docker compose up` build.
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
