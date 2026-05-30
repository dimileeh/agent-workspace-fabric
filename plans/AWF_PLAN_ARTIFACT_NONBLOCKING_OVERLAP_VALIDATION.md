# AWF Plan Artifact Nonblocking Overlap Validation

## Result

Implemented. AWF internal plan artifacts under `docs/awf-plans` are now removed
from inter-workspace dependency comparisons while remaining persisted and
visible in raw workspace `owned_paths`.

## What Changed

- Added `src/awf/common/owned_paths.py` with shared owned-path normalization and
  internal plan-artifact classification.
- Reused the normalizer from repository owned-path overlap matching.
- Filtered `docs/awf-plans` paths from:
  - merge-queue older-candidate blocker checks;
  - active owned-path overlap warning lookup;
  - lock overlap-risk computation;
  - overlap graph path matches and edges.
- Kept raw owned paths visible in locks and overlap graph nodes.
- Kept staleness plan-artifact behavior advisory and non-blocking.

## Regression Evidence

Before implementation, the new focused tests failed because
`docs/awf-plans/**` created:

- `MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE` blockers;
- `OWNED_PATH_OVERLAP_RISK` workspace warnings;
- lock overlap risks;
- overlap graph edges.

After implementation, the same tests pass and real overlaps still block/report.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/service/test_overlap_graph.py tests/unit/api/test_locks.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_empty_requested_owned_paths_do_not_report_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_non_overlapping_owned_paths_do_not_report_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_internal_plan_artifact_filter_does_not_hide_real_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_real_docs_owned_paths_still_report_overlap tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_plan_artifact_only_overlap_is_advisory_without_target_advanced tests/unit/service/test_staleness_parts/test_staleness_part_001.py::TestEvaluateStaleness::test_mixed_plan_artifact_and_source_overlap_blocks_on_source tests/unit/service/test_staleness_parts/test_staleness_part_002.py::TestStalenessRefreshService::test_plan_artifact_only_refresh_records_advisory_without_stale_candidate -q
```

Result: `46 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py src/awf/db/repositories/base.py src/awf/db/repositories/workspace_repo.py src/awf/service/merge_queue.py src/awf/service/locks.py src/awf/service/overlap_graph.py src/awf/service/staleness.py tests/unit/common/test_owned_paths.py tests/unit/runtime/test_merge_queue_ordering.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/api/test_locks.py tests/unit/service/test_overlap_graph.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/owned_paths.py src/awf/db/repositories/base.py src/awf/db/repositories/workspace_repo.py src/awf/service/merge_queue.py src/awf/service/locks.py src/awf/service/overlap_graph.py src/awf/service/staleness.py
```

Result: `Success: no issues found in 7 source files`.

## Rollout Note

No migration is needed. Existing raw `owned_paths` can remain unchanged. Live
merge blockers and overlap graph edges will recompute correctly after the AWF
service is rebuilt/restarted with this code.
