"""Owned-path classification helper tests."""

from __future__ import annotations

import re

import pytest

from awf.common import (
    ids,
    owned_paths,
)
from awf.common.owned_paths import (
    has_wildcard,
    internal_plan_artifact_owned_paths_from_profile,
    interworkspace_owned_paths,
    is_internal_plan_artifact_owned_path,
    is_under_internal_plan_artifact_dir,
    normalize_owned_path,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "normalized"),
    [
        (" ./docs//awf-plans/../awf-plans/ws.md ", "docs/awf-plans/ws.md"),
        ("../docs/README.md", "docs/README.md"),
        ("docs\\awf-plans\\**", "docs/awf-plans/**"),
        ("", ""),
    ],
)
def test_normalize_owned_path(raw_path: str, normalized: str) -> None:
    """Owned path normalization removes shell-ish path noise."""
    assert normalize_owned_path(raw_path) == normalized


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/awf/**", True),
        ("docs/[ab].md", True),
        ("tests/unit/test_?.py", True),
        ("src/awf/service/staleness.py", False),
    ],
)
def test_public_wildcard_helper(path: str, expected: bool) -> None:
    """Public wildcard helper identifies fnmatch pattern syntax."""
    assert has_wildcard(path) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/awf-plans/ws_123.md",
        "docs/awf-plans/ws_0123456789abcdef01234567.md",
        "./docs/awf-plans/ws_123.conformance.json",
        "./docs/awf-plans/ws_0123456789abcdef01234567.conformance.json",
        "docs/awf-plans/ws_123.json",
        "docs/awf-plans/ws_0123456789abcdef01234567.json",
        "docs/awf-plans/ws_other.md",
        "docs/awf-plans/ws_other.conformance.json",
        "docs/awf-plans/ws_protocol.md",
        "docs/awf-plans/ws_v2.md",
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
@pytest.mark.parametrize(
    "path",
    [
        "docs/awf-plans",
        "docs/awf-plans/",
        "docs/awf-plans/ws_x.md",
        "apps/console/docs/awf-plans/ws_x.md",
        "apps/console/docs/awf-plans/",
        "deep/a/b/docs/awf-plans/x.json",
        "./apps/console/docs/awf-plans/ws_x.md",
        # A nested README is gitignored and not the tracked canonical file, so it
        # is still a stray artifact that stays matched (#620).
        "apps/console/docs/awf-plans/README.md",
    ],
)
def test_is_under_internal_plan_artifact_dir_matches_dir_and_descendants_any_depth(
    path: str,
) -> None:
    """The internal plan dir and anything beneath it match at ANY depth (#620)."""
    assert is_under_internal_plan_artifact_dir(path) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/awf-plans/README.md",
        "./docs/awf-plans/README.md",
    ],
)
def test_is_under_internal_plan_artifact_dir_exempts_canonical_root_readme(
    path: str,
) -> None:
    """The tracked canonical root README is exempt so a stray untracked copy is flagged.

    The dirty guard suppresses matches here on untracked paths unconditionally,
    so a genuinely untracked ``docs/awf-plans/README.md`` must stay visible
    rather than be silently hidden.
    """
    assert is_under_internal_plan_artifact_dir(path) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "docs/awf-plans-archive/keep.md",
        "docs/awf-plansx/y.md",
        "docs/README.md",
        "",
        "xdocs/awf-plans/y.md",
        "awf-plans/y.md",
    ],
)
def test_is_under_internal_plan_artifact_dir_rejects_siblings_and_unrelated(path: str) -> None:
    """Sibling dirs (``docs/awf-plans-archive``) and unrelated paths are not matched."""
    assert is_under_internal_plan_artifact_dir(path) is False


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
            "docs/awf-plans/ws_protocol.md",
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
def test_disabled_planning_custom_profile_paths_are_not_internal_artifacts() -> None:
    """Custom planning paths are artifacts only when profile planning is required."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {
            "planning": {
                "required": False,
                "plan_path": "docs/{workspace_id}.md",
                "conformance_report_path": "docs/{workspace_id}.json",
            },
        },
    )

    assert internal_paths == ()
    assert interworkspace_owned_paths(
        [
            "docs/ws_0123456789abcdef01234567.md",
            "docs/ws_0123456789abcdef01234567.json",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "docs/ws_0123456789abcdef01234567.md",
        "docs/ws_0123456789abcdef01234567.json",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "resolved_profile",
    [
        None,
        [],
        {"planning": []},
        {"planning": {"required": True, "plan_path": "docs/shared-plan.md"}},
    ],
)
def test_profiles_without_workspace_templates_do_not_define_internal_artifacts(
    resolved_profile: object,
) -> None:
    """Only required planning templates with workspace placeholders are artifacts."""
    assert internal_plan_artifact_owned_paths_from_profile(resolved_profile) == ()


@pytest.mark.unit
def test_custom_profile_plan_artifact_paths_are_filtered_from_interworkspace_paths() -> None:
    """Resolved profile planning paths extend internal artifact classification."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {
            "planning": {
                "required": True,
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
def test_known_workspace_custom_plan_template_preserves_artifact_wildcards() -> None:
    """Known custom templates still filter persisted generated artifact globs."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"required": True, "plan_path": "docs/alternate/{workspace_id}.md"}},
        workspace_id="ws_0123456789abcdef01234567",
    )

    assert internal_paths == ("docs/alternate/ws_0123456789abcdef01234567.md",)
    assert interworkspace_owned_paths(
        [
            "docs/alternate/ws_*.md",
            "docs/alternate/ws_0123456789abcdef01234567.md",
            "docs/alternate/ws_76543210fedcba76543210fe.md",
            "docs/alternate/ws_protocol.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "docs/alternate/ws_76543210fedcba76543210fe.md",
        "docs/alternate/ws_protocol.md",
    )


@pytest.mark.unit
def test_custom_profile_plan_parent_scope_remains_interworkspace_owned() -> None:
    """Real files in a custom plan artifact parent directory keep overlap checks."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"required": True, "plan_path": "docs/runbooks/{workspace_id}.md"}},
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
        {"planning": {"required": True, "plan_path": "docs/{workspace_id}.md"}},
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
def test_unknown_custom_plan_template_keeps_real_ws_docs() -> None:
    """Unknown workspace ids filter generated artifacts without hiding real docs."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"required": True, "plan_path": "docs/{workspace_id}.md"}},
    )

    assert internal_paths == ("docs/ws_*.md",)
    assert interworkspace_owned_paths(
        [
            "docs/ws_0123456789abcdef01234567.md",
            "docs/ws_protocol.md",
            "docs/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == ("docs/ws_protocol.md", "docs/README.md")


@pytest.mark.unit
def test_unknown_custom_plan_template_does_not_filter_shorthand_workspace_ids() -> None:
    """Unknown custom artifact paths only match generated workspace IDs."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {
            "planning": {
                "required": True,
                "plan_path": "docs/alternate/{workspace_id}.md",
                "conformance_report_path": "docs/alternate/{workspace_id}.json",
            },
        },
    )

    assert internal_paths == ("docs/alternate/ws_*.md", "docs/alternate/ws_*.json")
    assert is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_0123456789abcdef01234567.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_0123456789abcdef01234567.json",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_123.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_123.json",
        internal_plan_artifact_paths=internal_paths,
    )
    assert interworkspace_owned_paths(
        [
            "docs/alternate/ws_0123456789abcdef01234567.md",
            "docs/alternate/ws_0123456789abcdef01234567.json",
            "docs/alternate/ws_123.md",
            "docs/alternate/ws_123.json",
            "docs/alternate/ws_protocol.md",
            "docs/alternate/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "docs/alternate/ws_123.md",
        "docs/alternate/ws_123.json",
        "docs/alternate/ws_protocol.md",
        "docs/alternate/README.md",
    )


@pytest.mark.unit
def test_configured_artifact_paths_ignore_empty_entries_and_match_wildcards() -> None:
    """Configured internal artifact paths tolerate empty entries and leaf wildcards."""
    internal_paths = ("", "docs/generated/*.json")

    assert is_internal_plan_artifact_owned_path(
        "docs/generated/report.json",
        internal_plan_artifact_paths=internal_paths,
    )
    assert interworkspace_owned_paths(
        ["docs/generated/report.json", "docs/generated/report.md"],
        internal_plan_artifact_paths=internal_paths,
    ) == ("docs/generated/report.md",)


@pytest.mark.unit
def test_workspace_id_glob_pattern_requires_workspace_id_glob() -> None:
    """Static configured artifact paths do not produce workspace-id regexes."""
    assert owned_paths._workspace_id_glob_path_pattern("docs/static.md") is None  # noqa: SLF001


@pytest.mark.unit
def test_custom_profile_workspace_id_glob_tracks_generated_id_contract() -> None:
    """Custom artifact globs share the same workspace-id contract as ID generation."""
    generated_workspace_id = ids.new_workspace_id()
    workspace_id_glob = owned_paths._WORKSPACE_ID_GLOB  # noqa: SLF001
    workspace_id_suffix_pattern = owned_paths._WORKSPACE_ID_SUFFIX_PATTERN  # noqa: SLF001

    assert re.fullmatch(ids.WORKSPACE_ID_PATTERN, generated_workspace_id)
    assert workspace_id_glob == f"{ids.WORKSPACE_ID_PREFIX}*"
    assert workspace_id_suffix_pattern == ids.WORKSPACE_ID_SUFFIX_PATTERN


@pytest.mark.unit
def test_default_reserved_plan_paths_still_filter_shorthand_workspace_ids() -> None:
    """The reserved default AWF plan directory keeps broad legacy matching."""
    assert is_internal_plan_artifact_owned_path(
        "docs/awf-plans/ws_123.md",
    )
    assert is_internal_plan_artifact_owned_path(
        "docs/awf-plans/ws_123.json",
    )
    assert interworkspace_owned_paths(
        [
            "docs/awf-plans/ws_123.md",
            "docs/awf-plans/ws_123.json",
            "docs/awf-plans/ws_protocol.md",
            "docs/alternate/README.md",
        ]
    ) == ("docs/alternate/README.md",)


@pytest.mark.unit
def test_repeated_workspace_id_placeholders_require_the_same_id() -> None:
    """Multi-placeholder templates do not hide cross-workspace path overlaps."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {
            "planning": {
                "required": True,
                "plan_path": "{workspace_id}/archive/{workspace_id}_report.md",
            },
        },
    )

    same_workspace_path = (
        "ws_0123456789abcdef01234567/archive/ws_0123456789abcdef01234567_report.md"
    )
    mixed_workspace_path = (
        "ws_0123456789abcdef01234567/archive/ws_76543210fedcba76543210fe_report.md"
    )

    assert internal_paths == ("ws_*/archive/ws_*_report.md", "ws_*/archive/**")
    assert is_internal_plan_artifact_owned_path(
        same_workspace_path,
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        mixed_workspace_path,
        internal_plan_artifact_paths=internal_paths,
    )
    assert interworkspace_owned_paths(
        [
            same_workspace_path,
            mixed_workspace_path,
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (mixed_workspace_path,)


@pytest.mark.unit
def test_custom_profile_parent_scope_filters_concrete_workspace_scope() -> None:
    """Generated custom parent scopes filter concrete workspace scope ownership."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"required": True, "plan_path": "{workspace_id}/plans/{workspace_id}.md"}},
    )

    assert internal_paths == ("ws_*/plans/ws_*.md", "ws_*/plans/**")
    assert is_internal_plan_artifact_owned_path(
        "ws_0123456789abcdef01234567/plans/**",
        internal_plan_artifact_paths=internal_paths,
    )
    assert is_internal_plan_artifact_owned_path(
        "ws_0123456789abcdef01234567/plans/ws_0123456789abcdef01234567.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "ws_0123456789abcdef01234567/plans/ws_76543210fedcba76543210fe.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "ws_0123456789abcdef01234567/plans/README.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert interworkspace_owned_paths(
        [
            "ws_0123456789abcdef01234567/plans/**",
            "ws_0123456789abcdef01234567/plans/ws_0123456789abcdef01234567.md",
            "ws_0123456789abcdef01234567/plans/ws_76543210fedcba76543210fe.md",
            "ws_0123456789abcdef01234567/plans/README.md",
        ],
        internal_plan_artifact_paths=internal_paths,
    ) == (
        "ws_0123456789abcdef01234567/plans/ws_76543210fedcba76543210fe.md",
        "ws_0123456789abcdef01234567/plans/README.md",
    )


@pytest.mark.unit
def test_workspace_scoped_custom_plan_parent_scope_is_internal_artifact() -> None:
    """Workspace-id parent directories may still be treated as artifact scopes."""
    internal_paths = internal_plan_artifact_owned_paths_from_profile(
        {"planning": {"required": True, "plan_path": "docs/runbooks/{workspace_id}/plan.md"}},
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
        "docs/alternate/workspace_0123456789abcdef01234567.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/workspace_protocol.md",
        internal_plan_artifact_paths=internal_paths,
    )
    assert not is_internal_plan_artifact_owned_path(
        "docs/alternate/ws_0123456789abcdef01234567.md",
        internal_plan_artifact_paths=internal_paths,
    )


@pytest.mark.unit
def test_interworkspace_owned_paths_deduplicates_filtered_paths() -> None:
    """Filtered paths are unique by normalized path with first spelling preserved."""
    assert interworkspace_owned_paths(
        [
            "src/awf/**",
            "docs/awf-plans/ws_123.md",
            "src/awf/**",
            "./src/awf/**",
            "docs/awf-plans/ws_456.json",
            "docs/runbooks/**",
            "docs/runbooks/**",
        ]
    ) == ("src/awf/**", "docs/runbooks/**")


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
