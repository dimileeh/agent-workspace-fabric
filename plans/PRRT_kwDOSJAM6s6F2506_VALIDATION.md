# PRRT_kwDOSJAM6s6F2506 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F2506_PLAN.md`

## Requirement Status

- `docs/awf-plans/**` is classified as an internal plan-artifact owned path:
  Complete. Covered by `tests/unit/common/test_owned_paths.py`.
- `interworkspace_owned_paths()` removes `docs/awf-plans/**`: Complete.
  Covered by `tests/unit/common/test_owned_paths.py`.
- Merge queue blockers ignore candidates that share only `docs/awf-plans/**`:
  Complete. Covered by focused merge queue ordering tests.
- Active overlap lookup and overlap graph ignore `docs/awf-plans/**`-only
  matches: Complete. Covered by repository overlap and overlap graph tests.
- Direct README ownership remains ordinary and overlapping when both sides
  declare `docs/awf-plans/README.md`: Complete. Covered by repository overlap,
  merge queue, and overlap graph tests.

## Evidence

Changed files:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `tests/unit/service/test_overlap_graph.py`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_awf_plans_readme_owned_paths_still_report_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_only_overlap_does_not_block_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_awf_plans_readme_overlap_blocks_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_overlap_does_not_hide_real_merge_queue_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_candidate_with_only_plan_artifact_path_does_not_block_merge_queue tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_internal_plan_artifact_only_edges tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_plan_artifact_matches_but_keeps_real_edge tests/unit/service/test_overlap_graph.py::test_overlap_graph_keeps_awf_plans_readme_overlap -q
```

Result before implementation: failed as expected with 9 failures showing
`docs/awf-plans/**` was still classified and compared as an ordinary
inter-workspace path.

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_awf_plans_readme_owned_paths_still_report_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_only_overlap_does_not_block_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_awf_plans_readme_overlap_blocks_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_overlap_does_not_hide_real_merge_queue_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_candidate_with_only_plan_artifact_path_does_not_block_merge_queue tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_internal_plan_artifact_only_edges tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_plan_artifact_matches_but_keeps_real_edge tests/unit/service/test_overlap_graph.py::test_overlap_graph_keeps_awf_plans_readme_overlap -q
```

Result: 32 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_plan_artifact_only_overlap_is_advisory_without_target_advanced tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_mixed_plan_artifact_and_source_overlap_blocks_on_source tests/unit/service/test_staleness_parts/test_staleness_part_002.py::TestStalenessRefreshService::test_plan_artifact_only_refresh_records_advisory_without_stale_candidate -q
```

Result: 4 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/service/test_overlap_graph.py
```

Result: All checks passed.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation, provenance, and merge gating after agent completion.
