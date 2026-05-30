# PRRT_kwDOSJAM6s6F3RF Custom Artifact Wildcards Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3RF_CUSTOM_ARTIFACT_WILDCARDS_PLAN.md`

## Requirement Status

- Preserve custom profile wildcard artifact scopes after workspace creation when
  a concrete workspace id is available: Complete.
- Continue filtering the concrete custom artifact path for the current
  workspace: Complete.
- Do not broaden custom wildcard matching to arbitrary `ws_` documentation names
  such as `ws_protocol.md`: Complete.
- Preserve workspace-specific parent directory artifact scope behavior:
  Complete.
- Run only focused validation for the changed helper; AWF/GitHub own broad
  validation after the agent phase: Complete.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/PRRT_kwDOSJAM6s6F3RF_CUSTOM_ARTIFACT_WILDCARDS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F3RF_CUSTOM_ARTIFACT_WILDCARDS_VALIDATION.md`

Pre-implementation regression check:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_known_workspace_custom_plan_template_preserves_artifact_wildcards -q`
  failed before the implementation because known custom profile rendering only
  produced the concrete artifact path. During implementation, existing
  regression coverage required preserving known-id real-doc behavior, so the
  final test asserts the persisted wildcard filtering behavior directly.

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_known_workspace_custom_plan_template_preserves_artifact_wildcards -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  passed with 37 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_known_requested_workspace_id_does_not_filter_other_ws_shaped_docs_path -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py::TestStalenessRefreshService::test_custom_sibling_plan_artifact_refresh_is_advisory_without_stale_candidate -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.

## Remaining Gaps

None.
