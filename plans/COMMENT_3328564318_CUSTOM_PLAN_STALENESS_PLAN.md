# Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F3LBK` reports that staleness target-change
classification renders custom planning artifact paths with the current
workspace id. For a profile such as `docs/alternate/{workspace_id}.md`, a
candidate that owns `docs/alternate/**` can see another workspace's merged
artifact as a blocking `STALE_OVERLAP` instead of advisory
`ADVISORY_PLAN_ARTIFACT_OVERLAP`.

Scope is limited to staleness target-change classification. Inter-workspace
owned-path filtering must remain narrowed to each workspace's concrete artifact
paths.

# Requirements Checklist

- Add a regression test for custom profile planning artifacts merged by a
  sibling workspace.
- Ensure staleness snapshots classify sibling custom planning artifacts as
  advisory plan artifact overlaps.
- Preserve existing inter-workspace owned-path filtering behavior.
- Run only focused local validation; AWF/GitHub own broad validation after the
  agent phase.

# Implementation Steps

1. Add a focused service-level staleness test using a custom planning profile
   and a broad owned path.
2. Confirm the regression fails before implementation.
3. Change staleness snapshot construction to use profile-derived wildcard
   planning artifact paths for target-change classification.
4. Re-run the focused staleness/common owned-path tests needed to prove the
   fix and guard the inter-workspace behavior.

# Assumptions/Changes

- Existing staleness tests used `ws_other` as a placeholder artifact id. The
  owned-path policy treats generated artifacts as `ws_` plus the generated
  workspace-id suffix shape, so fixture paths should use generated-format ids
  rather than broadening classification to arbitrary `ws_*` docs names.

# Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py::<test> -q`
  initially fails with a blocking overlap before the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py::<test> -q`
  passes after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_custom_profile_plan_artifact_paths_are_filtered_from_interworkspace_paths tests/unit/common/test_owned_paths.py::test_known_workspace_custom_plan_template_does_not_filter_other_ws_docs -q`
  passes, preserving narrowed inter-workspace filtering.
