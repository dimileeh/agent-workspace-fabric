# Comment 3330066374 Remonitor Current Head Settle Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F7p9n` reports that remonitor past-settle
head selection treats stale elapsed settle markers as evidence that the live
PR head is also past reviewer settle. The scope is limited to remonitor
settle-head selection, focused regression coverage, and the matching
validation record.

## Requirements Checklist

- When the current PR head is known, only classify remonitor as past-settle
  for that head if persisted settle state says that same head elapsed.
- Do not append or freeze a current candidate head merely because an older
  head has an elapsed marker.
- Preserve fallback behavior for cases where no current head is known and
  persisted elapsed markers are the only available evidence.
- Preserve warning/result/event behavior for genuine current-head
  past-settle remonitors.
- Run focused local checks only; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a focused regression for the pure remonitor settle-head helper proving
   stale elapsed markers do not arm a known current head.
2. Update existing service/API remonitor cases so current-head past-settle
   expectations use a current-head elapsed marker, not an older-head marker.
3. Update `remonitor_elapsed_settle_head_shas()` to prefer current-head
   evidence when `current_head_sha` is known and fall back to persisted marker
   scanning only when no current head is known.
4. Run targeted tests for the helper and remonitor cases touched.
5. Record focused validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k remonitor_elapsed_settle_head_shas`
  - The new helper regression fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_arms_current_candidate_head or remonitor_no_reason_stale_past_settle_does_not_arm_current_candidate_head or remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head or remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale"`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k remonitor_reopens_failed_candidate_with_latest_head`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/operator_hints.py tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
  - Passes after implementation.

Full AWF/GitHub validation, coverage gates, and CI-equivalent checks are not
run during this agent phase.
