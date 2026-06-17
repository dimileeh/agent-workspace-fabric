# Validation: PRRT_kwDOSJAM6s6KOFUR — Deposit planning artifacts on validation stop paths

## Plan reference

`plans/PRRT_kwDOSJAM6s6KOFUR_PLAN.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | When `_finish_validation_callback_if_terminal()` returns `True` inside `run_validation_and_fix_cycle()`, deposit planning artifacts before the function returns `stop=True`. | Complete | `src/awf/control/executor/execution_validation.py`: `_deposit_planning_artifacts_if_required()` helper defined after profile resolution and invoked at lines 223 (stale start transition), 289 (stale recheck), and 518 (stale callback after normal cleanup). |
| 2 | Preserve existing ordering invariant: deposit before any terminal status update that bumps `workspace.updated_at`. | Complete | The stale-callback returns do not call `_mark_failed`/`enter_blocked_for_protected_violation`; the new deposit runs immediately before the `ExecutionValidationResult(stop=True, ...)` return. Existing preserving helpers (`_mark_failed_preserving_planning_artifacts`, `_enter_blocked_preserving_planning_artifacts`, `_fail_validation_worktree_guard`) already order deposit before status change. |
| 3 | Do not run a redundant deposit on the normal success path. | Complete | The normal success path keeps its inline deposit gated on `planning_validation_handoff is None` (line 751). The new helper is only invoked on callback-terminal/stale paths. Tests assert `calls == ["deposit"]` for those stop paths only. |
| 4 | Gate the deposit on `profile.planning.required` via existing helper. | Complete | `_deposit_planning_artifacts_if_required()` delegates to `_planning_artifacts._deposit_planning_artifacts_best_effort()`, which returns early when `profile` is `None` or `not profile.planning.required`. |
| 5 | Add or update regression tests proving deposit order for callback-terminal paths. | Complete | `test_executor_coverage_edges_part_003.py`: updated `test_execution_validation_returns_stop_when_start_transition_is_stale` and `test_execution_validation_returns_stop_when_validate_recheck_is_stale` to assert deposit + artifact contents. `test_executor_coverage_edges_part_007.py`: added `test_execution_validation_deposits_artifacts_before_stale_callback_after_normal_cleanup`. |
| 6 | Keep changes minimal and scoped. | Complete | Only `execution_validation.py` and the two focused edge-coverage test files are modified. No unrelated refactor. |

## Implementation evidence

### `src/awf/control/executor/execution_validation.py`

- Moved profile resolution before the start-transition check so `_deposit_planning_artifacts_if_required()` has access to `profile` even on the stale-transition return.
- Added `_deposit_planning_artifacts_if_required()` helper (lines 199–214) that calls `_planning_artifacts._deposit_planning_artifacts_best_effort()` with the resolved `profile`, `workspace_id`, and `worktree_path`.
- Invoked the helper on three callback-terminal/stale stop paths:
  1. Stale start transition (line 223).
  2. Stale recheck inside validation loop (line 289).
  3. Stale callback after normal cleanup (line 518).

### `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`

- Added `json` import and `executor_service_artifacts` import.
- Updated `test_execution_validation_returns_stop_when_start_transition_is_stale`:
  - Creates a `planning.required=True` profile, writes plan + conformance files, spies on deposit, asserts `result.stop`, deposit call, and that both `plan.md` and `conformance.json` land in the served artifact dir.
- Updated `test_execution_validation_returns_stop_when_validate_recheck_is_stale`:
  - Same assertions as above for the stale recheck path.

### `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`

- Extracted `_workspace()` helper to reduce duplication.
- Added `test_execution_validation_deposits_artifacts_before_stale_callback_after_normal_cleanup`:
  - Validation passes, cleanup is OK, then `_finish_validation_callback_if_terminal` returns `True`; asserts deposit ran and both served artifacts exist.

## Verification commands and results

Focused unit tests (all pass):

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py \
  tests/unit/control/test_validation_worktree_guard_deposit_ordering.py \
  tests/unit/control/test_planning_artifacts_deposit_ordering.py \
  -q
```

Result: `24 passed in 0.95s`

Lint/type checks (all pass):

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/control/executor/execution_validation.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py

uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py
```

Result: `All checks passed!` / `Success: no issues found in 1 source file`

## Gaps / follow-ups

None. All planned requirements are satisfied. Full repository test/build suites are owned by AWF/GitHub CI per the workspace contract and were not run locally.
