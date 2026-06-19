# Validation: AWF-authored post-validation conformance report must not dirty worktree (#604)

Plan reference: `plans/WS_D7E7539D5D2E4DB8BFEED3A5_PLAN.md`

## Requirement-by-requirement status

| Requirement | Status | Evidence |
|---|---|---|
| Satisfied report written by AWF must not remain as a tracked dirty file at pre-push validation time | Complete | `_run_post_validation_conformance_check` in `src/awf/control/executor/planning_conformance.py` first runs `git restore --source=<base_commit> --worktree --staged -- <handoff.report_path>` to put back tracked base-commit content. If that restore succeeds but still leaves the path dirty, it tries a conditional `HEAD` restore to preserve the current committed report state. If restore fails or cannot clean the path, it falls back to unlinking `worktree_path / handoff.report_path`. The cleanup records the satisfied event only after the report path is clean, or returns a cleanup failure if unlink leaves it dirty. |
| Keep the PR monitor dirty-worktree guard strong | Complete | No changes to `check_validation_worktree_clean`, `INTERNAL_PLAN_ARTIFACT_PREFIX`, `changed_paths_are_only_internal_plan_artifacts`, or the PR monitor. Real source/config/test dirty paths still fail validation. |
| Preserve fresh report from agent (stdout or disk) | Complete | Both stdout-derived and fresh-on-disk reports still record the conformance event and then remove the on-worktree copy before returning success. Tests updated to assert the file no longer exists on the worktree. |
| Preserve real conformance failure path | Complete | The unlink only happens on the `report.satisfied` success branch. Unsatisfied reports are intentionally left on disk for diagnosis, and the failure return path is unchanged. |
| Preserve OSError on write as non-fatal | Complete | The existing write try/except is unchanged. The new unlink try/except is also non-fatal and logs a warning. |
| Add focused regression coverage for tracked pre-existing report path | Complete | `_GitRestoreFakeRunner` in `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py` simulates tracked restore behavior: a successful restore leaves the tracked file on disk with committed content and a clean worktree, while untracked paths fall back to unlink. Added `test_post_validation_conformance_unlink_failure_is_non_fatal` in `tests/unit/control/test_planning_ops_branch_edges.py`; updated satisfied-report tests assert the appropriate restore or unlink behavior for their tracked/untracked setup. |
| Console artifact visibility preserved | Complete | `src/awf/control/executor/execution_validation.py` now calls `_planning_artifacts._deposit_planning_artifacts_best_effort` on the conformance-success branch BEFORE the report is removed from the worktree, so the served artifact dir still receives the conformance report. |

## Commands run

Targeted tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py tests/unit/control/test_planning_ops_branch_edges.py -q
```

Result: `63 passed in 1.41s`.

Lint/type:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/executor/execution_validation.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/executor/execution_validation.py
```

Result: All checks passed / no issues found.

## Files changed

- `src/awf/control/executor/planning_ops.py`
- `src/awf/control/executor/execution_validation.py`
- `src/awf/control/executor/planning_artifacts.py` (reviewed; no changes required — deposit works from the worktree copy before removal)
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py`
- `tests/unit/control/test_planning_ops_branch_edges.py`
- `plans/WS_D7E7539D5D2E4DB8BFEED3A5_PLAN.md`
- `plans/WS_D7E7539D5D2E4DB8BFEED3A5_VALIDATION.md`
- `docs/awf-plans/ws_d7e7539d5d2e4db8bfeed3a5.conformance.json`

## No remaining gaps

All planned requirements are satisfied. No explicit deferrals.
