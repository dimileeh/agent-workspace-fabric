# No-Reason Past-Settle Remonitor Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F5x8` reports that `REMONITOR_PAST_SETTLE`
is emitted only when an operator supplies a non-empty remonitor reason. A
no-reason remonitor for a PR workspace already past reviewer settle should still
warn the operator that auto-merge timing is being re-entered.

Scope is limited to `WorkspaceControlService.remonitor_workspace`, focused
service regression coverage, and this plan/validation record.

## Requirements Checklist

- Detect past-settle remonitor state independently of whether `reason` is
  present.
- Emit `REMONITOR_PAST_SETTLE` in the control response, operation result, and
  remonitor event for no-reason past-settle remonitor.
- Re-arm existing initial-review and reviewer-settle state for no-reason
  past-settle remonitor so the monitor gets a fresh settle evaluation.
- Do not persist a pending operator hint when the remonitor reason is blank or
  omitted.
- Preserve existing operator-hint behavior for non-empty reasons.
- Run only targeted checks; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add a failing service lifecycle regression for monitoring-PR no-reason
   remonitor after reviewer settle.
2. Refactor remonitor state setup so elapsed settle detection happens before
   the optional operator-hint branch.
3. Emit warning and re-arm settle state whenever elapsed settle exists, while
   keeping pending-hint persistence conditional on non-empty reasons.
4. Run the focused regression and a narrow lint check for touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k no_reason_past_settle`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k remonitor`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passes after implementation.
