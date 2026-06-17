# Validation: Restore tracked conformance reports instead of staging deletion

Plan reference: `plans/PRRT_kwDOSJAM6s6KG6cH_PLAN.md`

## Requirement-by-requirement status

| Requirement | Status | Evidence |
|-------------|--------|----------|
| For a tracked conformance report, the worktree ends clean (no staged or unstaged deletion) after cleanup. | Complete | `src/awf/control/executor/planning_ops.py` now issues `git restore --source=HEAD --worktree --staged -- <report>` followed by `Path.unlink`. This restores the tracked path to its committed index/worktree state before removing the on-worktree copy, leaving no porcelain entry. |
| For an untracked/gitignored conformance report, the on-worktree file is still removed. | Complete | The new flow attempts restore first, logs on failure, then always unlinks. `test_satisfied_post_validation_conformance_report_untracked_fallback_to_unlink` passes. |
| Existing regression tests for tracked/untracked cases updated to assert new clean behavior. | Complete | `test_executor_coverage_edges_part_001.py`: `_GitRmFakeRunner` renamed to `_GitRestoreFakeRunner` and now intercepts `git restore`; all six affected tests now assert a `git restore` call and assert no plain `git rm` call. |
| No `git add` or `git commit` runs for the AWF artifact. | Complete | All updated tests retain `assert all("add" not in call.args ...)` and `assert all("commit" not in call.args ...)`. |
| Coverage is preserved or improved; new code paths covered. | Complete | The changed lines in `planning_ops.py` are exercised by the six updated conformance tests plus the two branch-edge tests. No new unreachable defensive branches introduced. |
| Focused test suite passes. | Complete | See commands below. |

## Commands run and results

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q
# 19 passed

uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py -q
# 56 passed

uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py
# Success: no issues found in 1 source file
```

## Files changed

- `src/awf/control/executor/planning_ops.py` — changed post-validation conformance cleanup from `git rm` (with unlink fallback) to `git restore --source=HEAD --worktree --staged` followed by `unlink`.
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py` — renamed fake runner, updated comments, and switched assertions from `git rm` to `git restore`.

## Gaps / next iterations

None. All planned requirements are satisfied.
