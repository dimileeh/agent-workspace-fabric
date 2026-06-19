# Pre-Push Recovered HEAD Protected Scope Validation

Plan reference: `plans/PRE_PUSH_RECOVERED_HEAD_PROTECTED_SCOPE_PLAN.md`

## Requirement Status

- Complete: When a missing pre-push HEAD is recovered to a different commit, the
  recovered delta is computed before validation.
- Complete: Recovered changed paths run agent runtime ownership repair before
  protected-scope repair.
- Complete: Recovered changed paths invoke the existing protected-scope repair hook before
  validation starts.
- Complete: Existing diff-unavailable fail-closed behavior is preserved.
- Complete: Protected-scope repair failure returns a structured pre-push validation failure
  and does not start validation.
- Complete: Local validation was kept focused; full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_runs_protected_scope_repair_before_validation -q`
  - Result: passed
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  - Result: passed, 9 tests
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Result: passed

Full AWF/GitHub validation was not run in the agent phase, per the workspace contract.
