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

    async def companion_image_exists(self, tag: str) -> bool:
        await asyncio.sleep(0)  # yield so concurrent callers can interleave
        self.exists_calls.append(tag)
        return self.exists_result

    async def build_companion_image(
        self,
        *,
        tag: str,
        build_context: str,
        dockerfile: str,
        labels: dict[str, str] | None = None,
    ) -> None:
        await asyncio.sleep(0)
        self.build_calls.append(
            {
                "tag": tag,
                "build_context": build_context,
                "dockerfile": dockerfile,
                "labels": dict(labels or {}),
            }
        )
        if self.build_error is not None:
            raise self.build_error
        self.exists_result = True  # a successful build makes the tag present


@pytest.mark.unit
def test_companion_image_tag_sanitizes_name_and_truncates_sha() -> None:
    tag = companion_image_tag("Aira Agent/Backend", "ABCDEF0123456789deadbeef")
    assert tag == "awf-companion-aira-agent-backend:abcdef012345"


@pytest.mark.unit
def test_companion_image_tag_falls_back_when_name_sanitizes_empty() -> None:
    assert companion_image_tag("///", "abc123").startswith("awf-companion-companion:")


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
    )

    assert tag == companion_image_tag("backend", "abc123def456")
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
    )

    assert tag == companion_image_tag("backend", "abc123def456")
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
            )
            for _ in range(4)
        )
    )

    expected = companion_image_tag("backend", "abc123def456")
    assert results == [expected, expected, expected, expected]
    assert len(compose.build_calls) == 1  # the per-tag lock collapses the wave
