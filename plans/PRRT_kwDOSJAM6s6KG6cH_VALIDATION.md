# Validation: Restore tracked conformance reports instead of staging deletion

Plan reference: `plans/PRRT_kwDOSJAM6s6KG6cH_PLAN.md`

## Requirement-by-requirement status

| Requirement | Status | Evidence |
|-------------|--------|----------|
| For a tracked conformance report, the worktree ends clean (no staged or unstaged deletion) after cleanup. | Complete | `src/awf/control/executor/planning_ops.py` now issues `git restore --source=base_commit --worktree --staged -- <report>`. This restores the tracked path to its pre-workspace baseline state in both the index and worktree, leaving the worktree clean. |
| For an untracked/gitignored conformance report, the on-worktree file is still removed. | Complete | The new flow attempts `git restore --source=base_commit` first, tries a conditional `HEAD` restore only if the first restore succeeds but leaves the path dirty, and falls back to `unlink` when restore cannot clean the path. `test_satisfied_post_validation_conformance_report_untracked_fallback_to_unlink` passes. |
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

- `src/awf/control/executor/planning_conformance.py` — changed post-validation conformance cleanup from `git rm` (with unlink fallback) to a primary `git restore --source=base_commit --worktree --staged`. For tracked reports the restore leaves the pre-workspace copy in place when that is clean; if the base restore still leaves the path dirty, a conditional `HEAD` restore preserves the current committed report state. For untracked/gitignored reports, restore fails and the code falls back to `unlink`.
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py` — renamed fake runner, updated comments, and switched assertions from `git rm` to `git restore`.

## Gaps / next iterations

None. All planned requirements are satisfied.
