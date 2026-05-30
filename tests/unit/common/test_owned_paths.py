"""Owned-path classification helper tests."""

from __future__ import annotations

import pytest

from awf.common import owned_paths
from awf.common.owned_paths import (
    internal_plan_artifact_owned_paths_from_profile,
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
        "docs/awf-plans/**",
        "./docs/awf-plans/**",
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
        "docs/awf-plans/README.md",
        "src/awf/**",
        "docs/runbooks/**",
    )


@pytest.mark.unit
def test_custom_profile_plan_artifact_paths_are_filtered_from_interworkspace_paths() -> None:
    """Resolved profile planning paths extend internal artifact classification."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {
            "planning": {
                "plan_path": "docs/alternate/{workspace_id}.md",
                "conformance_report_path": "docs/alternate/{workspace_id}.json",
            },
        },
        workspace_id="ws_custom",
    )

    assert "docs/alternate/**" not in internal_paths
    assert is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_custom.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_custom.json",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_*.json",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/README.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_custom.notes.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert interworkspace_owned_paths(
        [
            "docs/alternate/**",
            "docs/alternate/ws_custom.md",
            "docs/alternate/ws_custom.json",
            "docs/alternate/ws_*.json",
            "docs/alternate/README.md",
            "docs/alternate/ws_custom.notes.md",
            "src/awf/**",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "docs/alternate/**",
        "docs/alternate/ws_*.json",
        "docs/alternate/README.md",
        "docs/alternate/ws_custom.notes.md",
        "src/awf/**",
    )


@pytest.mark.unit
def test_custom_profile_plan_parent_scope_remains_interworkspace_owned() -> None:
    """Real files in a custom plan artifact parent directory keep overlap checks."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"plan_path": "docs/runbooks/{workspace_id}.md"}},
        workspace_id="ws_custom",
    )

    assert internal_paths == ("docs/runbooks/ws_custom.md",)
    assert interworkspace_owned_paths(
        [
            "docs/runbooks/**",
            "docs/runbooks/ws_*.md",
            "docs/runbooks/ws_custom.md",
            "docs/runbooks/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == ("docs/runbooks/**", "docs/runbooks/ws_*.md", "docs/runbooks/README.md")


@pytest.mark.unit
def test_known_workspace_custom_plan_template_does_not_filter_other_ws_docs() -> None:
    """Known workspace ids narrow custom artifact filtering to the concrete path."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"plan_path": "docs/{workspace_id}.md"}},
        workspace_id="ws_custom",
    )

    assert internal_paths == ("docs/ws_custom.md",)
    assert interworkspace_owned_paths(
        [
            "docs/ws_custom.md",
            "docs/ws_protocol.md",
            "docs/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == ("docs/ws_protocol.md", "docs/README.md")


@pytest.mark.unit
def test_unknown_workspace_custom_plan_template_keeps_ws_glob() -> None:
    """Unknown workspace ids retain broad artifact matching for pre-id checks."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"plan_path": "docs/{workspace_id}.md"}},
    )

    assert internal_paths == ("docs/ws_*.md",)
    assert interworkspace_owned_paths(
        [
            "docs/ws_generated.md",
            "docs/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == ("docs/README.md",)


@pytest.mark.unit
def test_workspace_scoped_custom_plan_parent_scope_is_internal_artifact() -> None:
    """Workspace-id parent directories may still be treated as artifact scopes."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"plan_path": "docs/runbooks/{workspace_id}/plan.md"}},
        workspace_id="ws_custom",
    )

    assert internal_paths == ("docs/runbooks/ws_custom/plan.md", "docs/runbooks/ws_custom/**")
    assert interworkspace_owned_paths(
        [
            "docs/runbooks/**",
            "docs/runbooks/ws_*/**",
            "docs/runbooks/ws_custom/plan.md",
            "docs/runbooks/ws_custom/**",
            "docs/runbooks/ws_custom/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "docs/runbooks/**",
        "docs/runbooks/ws_*/**",
        "docs/runbooks/ws_custom/README.md",
    )


@pytest.mark.unit
def test_workspace_id_glob_matching_uses_configured_glob_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured workspace-id glob prefixes stay synchronized with matching."""
    monkeypatch.setattr(owned_paths, "_WORKSPACE_ID_GLOB", "workspace_*")

    internal_paths = ("docs/alternate/workspace_*.md",)

    assert is_internal_plan_artifact_owned_path(
        "docs/alternate/workspace_123.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_123.md",
        internal_plan_artifact_paths=internal_paths,
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
