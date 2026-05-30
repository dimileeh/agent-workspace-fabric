"""Owned-path classification helper tests."""

from __future__ import annotations

import pytest

from awf.common import owned_paths
from awf.common.owned_paths import (
    interworkspace_owned_paths,
    is_internal_plan_artifact_owned_path,
    normalize_owned_path,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "normalized"),
    [
        (" ./docs//awf-plans/../awf-plans/ws.md ", "docs/awf-plans/ws.md"),
        ("docs\\awf-plans\\**", "docs/awf-plans/**"),
        ("", ""),
    ],
)
def test_normalize_owned_path(raw_path: str, normalized: str) -> None:
    """Owned path normalization removes shell-ish path noise."""
    assert normalize_owned_path(raw_path) == normalized


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/awf-plans/ws_123.md",
        "./docs/awf-plans/ws_123.conformance.json",
        "docs/awf-plans/ws_123.json",
        "docs/awf-plans/ws_*.md",
        "docs/awf-plans/ws_*.conformance.json",
        "docs/awf-plans/ws_*.json",
    ],
)
def test_internal_plan_artifact_owned_paths_are_classified(path: str) -> None:
    """AWF internal plan artifact paths are classified as nonblocking."""
    assert is_internal_plan_artifact_owned_path(path) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/**",
        "docs/runbooks/**",
        "plans/**",
        "docs/awf-plans",
        "docs/awf-plans/**",
        "docs/awf-plans/README.md",
        "docs/awf-plans-other/**",
        "docs/awf-plans/ws_123.notes.md",
        "docs/awf-plans/ws_123.notes.json",
        "docs/awf-plans/nested/ws_123.md",
    ],
)
def test_real_docs_and_repo_plan_paths_are_not_internal_plan_artifacts(path: str) -> None:
    """Nearby repository documentation paths remain ordinary owned paths."""
    assert is_internal_plan_artifact_owned_path(path) is False


@pytest.mark.unit
def test_interworkspace_owned_paths_filters_only_internal_plan_artifacts() -> None:
    """Inter-workspace filtering drops only internal plan artifact paths."""
    assert interworkspace_owned_paths(
        [
            "",
            "docs/awf-plans/ws_*.md",
            "docs/awf-plans/ws_123.conformance.json",
            "docs/awf-plans/ws_123.json",
            "docs/awf-plans/**",
            "docs/awf-plans/README.md",
            "src/awf/**",
            "docs/runbooks/**",
            "./docs/awf-plans/ws_456.md",
            "./docs/awf-plans/ws_*.json",
        ]
    ) == (
        "docs/awf-plans/**",
        "docs/awf-plans/README.md",
        "src/awf/**",
        "docs/runbooks/**",
    )


@pytest.mark.unit
def test_interworkspace_owned_paths_normalizes_each_path_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inter-workspace filtering reuses each normalized path for classification."""
    calls: list[str] = []
    real_normalize_owned_path = owned_paths.normalize_owned_path

    def counting_normalize_owned_path(path: str) -> str:
        """Record normalization calls while preserving real normalization behavior."""
        calls.append(path)
        return real_normalize_owned_path(path)

    monkeypatch.setattr(owned_paths, "normalize_owned_path", counting_normalize_owned_path)

    paths = (
        "",
        "docs/awf-plans/ws_123.json",
        "src/awf/**",
        "./docs/awf-plans/ws_456.md",
    )

    assert owned_paths.interworkspace_owned_paths(paths) == ("src/awf/**",)
    assert calls == list(paths)
