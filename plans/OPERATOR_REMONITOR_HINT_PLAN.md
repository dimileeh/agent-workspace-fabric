# Operator Remonitor Hint Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F5wos` reports that operator remonitor reasons are only persisted when reviewer settle has already elapsed. The remonitor endpoint should preserve any non-empty operator hint while only applying the auto-merge freeze warning after the settle window has elapsed.

Scope is limited to `WorkspaceControlService.remonitor_workspace`, focused regression coverage, and this plan/validation record.

## Requirements Checklist

- Persist a pending operator hint for `monitoring_pr` workspaces whenever a non-empty remonitor reason is provided.
- Keep the past-settle auto-merge freeze and `REMONITOR_PAST_SETTLE` warning conditional on elapsed settle state.
- Preserve existing remonitor claim reset, operation payload, event payload, and idempotency behavior.
- Add a regression test for a pre-settle remonitor reason proving the hint is persisted without freeze/warning state.
- Run only targeted checks for the changed behavior; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a failing service lifecycle regression test for pre-settle operator hints.
2. Update remonitor hint persistence so hint creation/persistence happens before the past-settle branch.
3. Keep freeze arming and warning emission inside the past-settle branch.
4. Run the focused regression test and a focused file-level test command if practical.
5. Record validation evidence in `plans/OPERATOR_REMONITOR_HINT_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k remonitor`
  - Passes after implementation.
- Initial targeted regression test should fail before implementation, proving the test covers the reported bug.
