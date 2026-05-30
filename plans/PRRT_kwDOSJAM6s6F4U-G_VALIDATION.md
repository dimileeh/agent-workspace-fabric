# PRRT_kwDOSJAM6s6F4U-G Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F4U-G_PLAN.md`

## Requirement Status

- Complete: Custom `{workspace_id}` wildcard artifact paths match only real generated workspace IDs.
  - Evidence: `src/awf/common/owned_paths.py` now constrains custom workspace-id glob regexes to `ws_` plus 24 lowercase hex characters.
- Complete: Shorthand `ws_123` matching remains supported for the reserved default `docs/awf-plans` artifact classifier.
  - Evidence: `tests/unit/common/test_owned_paths.py::test_default_reserved_plan_paths_still_filter_shorthand_workspace_ids`.
- Complete: Staleness treats a target-branch change to a custom-profile-owned real shorthand path as blocking `STALE_OVERLAP`.
  - Evidence: `tests/unit/service/test_staleness_parts/test_staleness_part_002.py::TestStalenessRefreshService::test_custom_plan_path_shorthand_target_change_is_blocking_overlap`.
- Complete: Existing concrete workspace-id matching and repeated-placeholder consistency remain intact.
  - Evidence: focused owned-path tests passed after updating placeholder fixtures to generated-ID shapes.

## Commands Run

- Failing before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py -k "custom_sibling_plan_artifact_refresh_is_advisory_without_stale_candidate or custom_plan_path_shorthand_target_change_is_blocking_overlap" -q`
- Passing after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py -k "custom_sibling_plan_artifact_refresh_is_advisory_without_stale_candidate or custom_plan_path_shorthand_target_change_is_blocking_overlap" -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py tests/unit/service/test_staleness_parts/test_staleness_part_002.py`
- Formatting:
  - `uv run --python 3.12 --extra dev ruff format tests/unit/common/test_owned_paths.py`

Full AWF/GitHub validation is managed by AWF after agent completion.
