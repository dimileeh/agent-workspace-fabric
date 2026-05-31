# Failed Remonitor Operator Hint Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F5wup` reports that operator remonitor hints are
persisted only when the workspace starts in `monitoring_pr`, even though
`failed` workspaces are also eligible for remonitor. A failed workspace
remonitored after reviewer settle with a non-empty reason must store the
operator hint, re-arm the auto-merge freeze, and emit `REMONITOR_PAST_SETTLE`.

Scope is limited to `WorkspaceControlService.remonitor_workspace`, focused
service regression coverage, and this plan/validation record.

## Requirements Checklist

- Persist non-empty remonitor reasons for every remonitor-eligible PR workspace,
  including `failed`.
- Preserve the failed-workspace reset back to `monitoring_pr`.
- Re-arm initial review grace and reviewer-settle state when the failed
  workspace remonitor is already past settle.
- Include the pending hint in the remonitor event and include the warning in the
  event, operation result, and control response.
- Run only targeted checks; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add a failing service lifecycle regression for failed-workspace past-settle
   remonitor with an operator reason.
2. Broaden remonitor hint persistence so it is based on remonitor eligibility
   and a non-empty reason, not only the `monitoring_pr` source state.
3. Keep freeze/warning emission conditional on elapsed settle state and PR head
   context.
4. Run the focused regression and a narrow lint check for touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k failed_workspace_past_settle`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passes after implementation.
