# Validation: PRRT_KKSZU conformance report restore cleanup

Plan reference: `plans/PRRT_KKSZU_conformance_report_restore_plan.md`

## Requirement-by-requirement status

| Requirement | Status | Evidence |
|-------------|--------|----------|
| 1. After a successful `git restore --source=base_commit`, if the report path is still dirty relative to HEAD, restore the report path from HEAD and verify cleanliness. | Complete | `src/awf/control/executor/planning_ops.py:423-447` now runs `git restore --source=HEAD --worktree --staged -- <report_path>` and checks `_report_path_is_dirty` before returning. |
| 2. If HEAD restore fails or path remains dirty, fall back to existing `unlink()` path and log as before. | Complete | `src/awf/control/executor/planning_ops.py:448-473` preserves the original warning and `unlink()` fallback. |
| 3. Preserve tracked/untracked behavior. | Complete | Existing tests `test_satisfied_post_validation_conformance_report_restores_tracked_report_from_base_commit` and `test_satisfied_post_validation_conformance_report_is_written_not_committed` still pass; untracked fallback unchanged. |
| 4. Update/add regression tests for staged modification and staged deletion residue. | Complete | `test_executor_coverage_edges_part_002.py`: added `test_satisfied_post_validation_conformance_report_restores_from_head_when_base_differs` and `test_satisfied_post_validation_conformance_report_unlinks_when_head_restore_fails`; `test_planning_ops_branch_edges.py`: added `test_post_validation_conformance_staged_deletion_restored_from_head`. |
| 5. Run focused unit tests, lint, and type checks. | Complete | See verification commands below. |

## Verification commands run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py -q tests/unit/control/test_planning_ops_branch_edges.py -q
# 42 passed

uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py tests/unit/control/test_planning_ops_branch_edges.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py
# Success: no issues found in 1 source file
```

Full repository validation is owned by AWF/GitHub CI after agent completion;
the broad suite was not run per workspace contract.

## Files changed

- `src/awf/control/executor/planning_ops.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`
- `tests/unit/control/test_planning_ops_branch_edges.py`
- `plans/PRRT_KKSZU_conformance_report_restore_plan.md`
- `plans/PRRT_KKSZU_conformance_report_restore_VALIDATION.md`

## Gaps / Deferrals

None.
