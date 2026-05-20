# PR #272 Git Classification Failure Retry Validation

Plan reference: `plans/PR272_GIT_CLASSIFICATION_FAILURE_RETRY_PLAN.md`

## Requirement Status

- Retryable git command failures use `state="failed"`: Complete.
  Evidence: `_classify_preserved_active_worktree` now returns failed
  classifications for branch, head, status, and ahead-count command failures.
- Failed classifications map to salvage-blocked during grace: Complete.
  Evidence: `_recover_preserved_active_execution` records
  `workspace.active_execution_salvage_blocked` with the failed classification
  while preservation grace remains.
- Semantic ambiguities still require operator recovery: Complete.
  Evidence: dirty worktree and missing branch-name regressions still pass and
  continue to assert `state="ambiguous"`.
- Persistent failures after grace still require operator recovery: Complete.
  Evidence: added an expired-grace status-failure regression that records
  operator-required recovery with `classification.state == "failed"`.
- Transient status failure retries into validate-only salvage: Complete.
  Evidence: added a regression where the first status command fails with an
  `index.lock` error, then the second scan validates the committed worktree
  without writing operator-required.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "git_status_failure_retries_during_grace"`
  - Failed before implementation with `runtime_preserved_operator_recovery_required`
    instead of `runtime_preserved_salvage_blocked`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "git_status_failure_retries_during_grace"`
  - Passed after implementation: `1 passed, 215 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "git_status_failure or ambiguous_dirty_worktree or missing_branch_name"`
  - Passed: `4 passed, 213 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passed: `217 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Passed.

## Gaps

None.
