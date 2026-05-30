# PRRT_kwDOSJAM6s6F28om Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F28om_PLAN.md`

## Requirement Status

- Custom profile planning scopes such as `docs/alternate/**` are filtered from
  inter-workspace owned-path comparisons when the resolved profile uses
  `docs/alternate/{workspace_id}` artifact templates: Complete.
- Custom generated artifact filenames such as `docs/alternate/ws_123.md` and
  `docs/alternate/ws_123.json` are recognized as internal when profile context
  is supplied: Complete.
- Real files under the same custom root, such as `docs/alternate/README.md`,
  remain ordinary owned paths: Complete.
- Existing default `docs/awf-plans` behavior and normalization guarantees stay
  intact: Complete.
- Focused tests and lint cover the touched helper and affected behavior:
  Complete.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `src/awf/db/repositories/workspace_repo.py`
- `src/awf/service/merge_queue.py`
- `src/awf/service/locks.py`
- `src/awf/service/overlap_graph.py`
- `src/awf/service/staleness.py`
- `src/awf/service/workspaces_create.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/common/test_owned_paths.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `tests/unit/service/test_locks.py`

Failing-before evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_custom_profile_plan_artifact_paths_are_filtered_from_interworkspace_paths -q`
  failed before the helper existed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate -q`
  failed because the custom artifact scope still blocked merge queue ordering.
- After the first implementation, the common custom-path test also failed on
  `docs/alternate/ws_custom.notes.md`, proving the matcher needed the same
  narrow generated-artifact semantics as the default classifier.

Passing focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_only_overlap_does_not_block_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_custom_plan_artifact_overlap_does_not_block_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_awf_plans_readme_overlap_blocks_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_overlap_does_not_hide_real_merge_queue_overlap -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_plan_artifact_only_overlap_is_advisory_without_target_advanced tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_mixed_plan_artifact_and_source_overlap_blocks_on_source -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_internal_plan_artifact_only_edges tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_plan_artifact_matches_but_keeps_real_edge tests/unit/service/test_overlap_graph.py::test_overlap_graph_keeps_awf_plans_readme_overlap -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_locks.py::test_overlap_risks_prefilters_candidate_owned_paths_once -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py src/awf/db/repositories/workspace_repo.py src/awf/service/merge_queue.py src/awf/service/locks.py src/awf/service/overlap_graph.py src/awf/service/staleness.py src/awf/service/workspaces_create.py src/awf/service/workspaces_retry.py tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/service/test_locks.py`
- `uv run --python 3.12 --extra dev mypy src/awf/common/owned_paths.py src/awf/db/repositories/workspace_repo.py src/awf/service/merge_queue.py src/awf/service/locks.py src/awf/service/overlap_graph.py src/awf/service/staleness.py src/awf/service/workspaces_create.py src/awf/service/workspaces_retry.py`

Full AWF/GitHub validation was intentionally not run inside the agent phase;
AWF owns broad validation, provenance, and merge gating after completion.

## Remaining Gaps

None.
