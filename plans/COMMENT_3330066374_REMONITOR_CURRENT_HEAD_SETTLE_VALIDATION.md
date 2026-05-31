# Comment 3330066374 Remonitor Current Head Settle Validation

Plan reference:
`plans/COMMENT_3330066374_REMONITOR_CURRENT_HEAD_SETTLE_PLAN.md`

## Requirement Status

- Complete: When the current PR head is known, remonitor past-settle
  selection now requires an elapsed marker for that same current head.
- Complete: Stale elapsed markers no longer append or freeze the current
  candidate head, and the service regression verifies no warning is emitted
  for that pre-settle current head case.
- Complete: Fallback persisted-marker scanning remains available when no
  current head is known.
- Complete: Genuine current-head past-settle warning/result/event behavior is
  preserved by the focused service/API regressions.
- Complete: Ran focused local checks only; full AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/operator_hints.py`
- `tests/unit/runtime/test_pr_monitor_operator_hints.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- `tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
- `plans/COMMENT_3330066374_REMONITOR_CURRENT_HEAD_SETTLE_PLAN.md`
- `plans/COMMENT_3330066374_REMONITOR_CURRENT_HEAD_SETTLE_VALIDATION.md`

Focused TDD evidence:

- Red before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k remonitor_elapsed_settle_head_shas`
  - Failed because stale-head elapsed state returned both the stale head and
    the current head.

Focused checks after implementation:

- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k remonitor_elapsed_settle_head_shas`
  - Result: `2 passed, 25 deselected`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_arms_current_candidate_head or remonitor_no_reason_stale_past_settle_does_not_arm_current_candidate_head or remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head or remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale"`
  - Result: `4 passed, 22 deselected`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k remonitor_reopens_failed_candidate_with_latest_head`
  - Result: `1 passed, 46 deselected`.
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
  - Result: `All checks passed!`.
- Passed:
  `git diff --check`
  - Result: no whitespace errors.

## Remaining Gaps

None for this thread. Broad validation, coverage gates, and CI-equivalent
checks are intentionally left to AWF/GitHub after this agent phase.
