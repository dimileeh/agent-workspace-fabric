# Remonitor Reopen Head Validation

Plan reference: `plans/REMONITOR_REOPEN_HEAD_PLAN.md`

## Requirement Status

- Reproduce the mismatch where `monitor_last_commit_sha` lags the latest merge-candidate
  `head_sha`: Complete.
- Preserve existing past-settle freeze behavior and warning payloads: Complete.
- Reopen failed-workspace merge candidates with the same latest candidate head used for
  remonitor settle/freeze evaluation, falling back to workspace monitor SHA only when no
  candidate head is known: Complete.
- Keep the fix scoped to remonitor control code and focused tests: Complete.
- Do not run broad AWF/GitHub validation in the agent phase: Complete.

## Evidence

Changed files:

- `src/awf/service/controls.py`
- `src/awf/service/controls_helpers.py`
- `tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`

Focused checks:

- Failed before implementation as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k remonitor_reopens_failed_candidate_with_latest_head`
  - Failure showed reopened candidate head `bbbb...` instead of latest candidate head `cccc...`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k "remonitor_reopens_failed_candidate_with_latest_head or remonitor_past_settle_persists_operator_hint_and_warns or remonitor_failed_workspace_with_pr_reopens_candidate_for_worker"`
  - Result: `3 passed, 49 deselected`.
- Passed focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/controls_helpers.py tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation was not run locally per the workspace contract; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.
