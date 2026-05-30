# CI PR 313 Fix Validation

Plan reference: `plans/CI_PR313_FIX_PLAN.md`

## Requirement Status

- Preserve AWF plan/conformance artifacts under `docs/awf-plans/ws_*` as advisory stale reasons when they overlap owned paths: Complete.
- Keep real source overlaps blocking when plan-artifact overlaps are mixed with source path overlaps: Complete.
- Preserve validation-phase status recheck behavior while updating the focused unit fixture for resolved-profile sync: Complete.
- Split oversized first-party test code into smaller modules without changing test behavior: Complete.
- Run only focused repro, targeted tests/lint, and leave broad AWF/GitHub validation to AWF: Complete.
- Commit all local changes on the current branch and do not push: Complete.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_003.py`
- `plans/CI_PR313_FIX_PLAN.md`
- `plans/CI_PR313_FIX_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_merge_queue_parts/test_merge_queue_part_001.py::TestMergeQueueListPart001::test_plan_artifact_advisory_reason_does_not_block_merge_queue tests/unit/api/test_merge_queue_parts/test_merge_queue_part_001.py::TestMergeQueueListPart001::test_mixed_plan_artifact_and_source_overlap_blocks_merge_queue tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_returns_stop_when_validate_recheck_is_stale tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/integration/test_parallel_candidate_stale_refresh.py::test_non_overlapping_docs_and_test_target_changes_remain_ready_when_policy_allows -q
```

Result: `5 passed in 4.16s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_003.py -q
```

Result: `50 passed in 7.26s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_003.py
```

Result: `All checks passed!`

Full AWF/GitHub validation, full coverage, and CI-equivalent commands were not run locally per the workspace contract; AWF owns those gates after agent completion.
