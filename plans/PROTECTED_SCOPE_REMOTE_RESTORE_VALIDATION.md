# Protected Scope Remote Restore Validation

Plan reference: `plans/PROTECTED_SCOPE_REMOTE_RESTORE_PLAN.md`

## Requirement Status

- Verify protected dirty tracked paths against the fetched remote PR branch tree:
  Complete. `_protected_scope_violations_not_restored_to_remote_branch` now
  uses `git diff --quiet FETCH_HEAD -- <path>`.
- Preserve fail-closed behavior when fetch or diff verification fails:
  Complete. Fetch and diff failures still raise `ProtectedScopeDiffError`, and
  existing fail-closed tests remain covered.
- Keep untracked protected paths blocked:
  Complete. The untracked-path branch in the helper was unchanged.
- Add or update focused regression coverage for the remote-advanced case:
  Complete. Added
  `test_protected_scope_revert_verifies_tracked_restore_against_fetch_head`.
- Keep the change scoped to PR monitor protected-scope restore verification:
  Complete. Production changes are limited to
  `src/awf/runtime/pr_monitor_runner.py`.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `plans/PROTECTED_SCOPE_REMOTE_RESTORE_PLAN.md`
- `plans/PROTECTED_SCOPE_REMOTE_RESTORE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_revert_verifies_tracked_restore_against_fetch_head -q`
  - Failed before implementation with `ProtectedScopeDiffError` from
    `merge-base FETCH_HEAD HEAD`.
  - Passed after implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passed: `130 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: `Success: no issues found in 155 source files`.
- `uv run --python 3.12 --extra dev pytest tests/unit -q`
  - Passed: `6291 passed`.

## Remaining Gaps

None.
