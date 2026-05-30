# PRRT_kwDOSJAM6s6F2uD7 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F2uD7_PLAN.md`

## Requirement Status

- `docs/awf-plans/README.md` is not classified as an internal artifact:
  Complete. Covered by `tests/unit/common/test_owned_paths.py` and staleness
  regression coverage.
- Broad directory ownership such as `docs/awf-plans/**` is not silently removed
  from inter-workspace comparisons: Complete. Covered by repository overlap,
  merge-queue ordering, and overlap graph README regressions.
- Generated artifact paths/globs remain internal: Complete. Covered by common
  helper tests and updated generated `ws_*` overlap tests.
- Staleness records README target changes as blocking overlap, while generated
  plan/conformance target changes remain advisory: Complete. Covered by focused
  staleness tests.
- Focused tests and lint cover the touched helper and behavior: Complete.

## Evidence

Changed files:

- `src/awf/common/owned_paths.py`
- `src/awf/service/staleness.py`
- `tests/unit/common/test_owned_paths.py`
- `tests/unit/service/test_staleness_parts/test_staleness_part_001.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `tests/unit/service/test_overlap_graph.py`
- `tests/unit/api/test_locks.py`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_awf_plans_readme_owned_paths_still_report_overlap -q
```

Result before implementation: failed as expected, with 8 failures and 11
passes. Failures showed `docs/awf-plans`, `docs/awf-plans/**`, and
`docs/awf-plans/README.md` were still classified as internal artifacts.

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_awf_plans_readme_owned_paths_still_report_overlap -q
```

Result: 19 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_plan_artifact_only_overlap_is_advisory_without_target_advanced tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_awf_plans_readme_overlap_blocks_as_real_docs_path tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_mixed_plan_artifact_and_source_overlap_blocks_on_source tests/unit/service/test_staleness_parts/test_staleness_part_002.py::TestStalenessRefreshService::test_plan_artifact_only_refresh_records_advisory_without_stale_candidate tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_real_docs_owned_paths_still_report_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_awf_plans_readme_owned_paths_still_report_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_only_overlap_does_not_block_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_awf_plans_readme_overlap_blocks_later_candidate tests/unit/runtime/test_merge_queue_ordering.py::test_plan_artifact_overlap_does_not_hide_real_merge_queue_overlap tests/unit/runtime/test_merge_queue_ordering.py::test_candidate_with_only_plan_artifact_path_does_not_block_merge_queue tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_internal_plan_artifact_only_edges tests/unit/service/test_overlap_graph.py::test_overlap_graph_ignores_plan_artifact_matches_but_keeps_real_edge tests/unit/service/test_overlap_graph.py::test_overlap_graph_keeps_awf_plans_readme_overlap tests/unit/api/test_locks.py::test_get_locks_ignores_internal_plan_artifact_only_overlap_risks tests/unit/api/test_merge_queue_parts/test_merge_queue_part_001.py::TestMergeQueueListPart001::test_plan_artifact_advisory_reason_does_not_block_merge_queue tests/unit/api/test_merge_queue_parts/test_merge_queue_part_001.py::TestMergeQueueListPart001::test_mixed_plan_artifact_and_source_overlap_blocks_merge_queue -q
```

Result: 35 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py src/awf/service/staleness.py tests/unit/common/test_owned_paths.py tests/unit/service/test_staleness_parts/test_staleness_part_001.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/service/test_overlap_graph.py tests/unit/api/test_locks.py
```

Result: All checks passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/owned_paths.py src/awf/service/staleness.py
```

Result: Success, no issues found in 2 source files.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation, provenance, and merge gating after agent completion.
