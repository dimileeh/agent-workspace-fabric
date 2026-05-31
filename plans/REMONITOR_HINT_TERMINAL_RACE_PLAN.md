# Remonitor Hint Terminal Race Plan

## Problem Statement And Scope

PR review comment `issue:4585104324` reports two PR monitor regressions in the
operator remonitor hint flow:

- `_MonitorAgentRuntimeOwnershipRepairFailedError` returns a terminal push
  result while leaving the active operator hint as `pending`.
- A concurrent remonitor hint or freeze can arrive after the merge critical
  section's single operator-state refresh and before `merge_pr()`.

Scope is limited to the PR monitor runner operator hint path, merge critical
section recheck, and focused unit regressions for those behaviors.

## Requirements Checklist

- Mark an operator hint `needs_human` before returning a terminal ownership
  repair failure from `_run_operator_hint_cycle`.
- Preserve the terminal push-result semantics and ownership-repair reason code.
- Re-read operator hint/freeze state immediately before `merge_pr()` when all
  other merge gates have passed.
- If the final re-read imports a pending hint, dispatch the hint repair instead
  of merging.
- If the final re-read imports freeze-only review grace or settle markers, wait
  instead of merging.
- Keep validation focused; do not run broad AWF/GitHub-owned validation.

## Implementation Steps

1. Add failing unit coverage for the ownership-repair failure branch marking the
   operator hint terminal.
2. Add failing unit coverage for a remonitor hint written between the existing
   merge-gate recheck and the final `merge_pr()` call.
3. Add failing unit coverage for freeze-only remonitor state written in the same
   final pre-merge window.
4. Update `_run_operator_hint_cycle` to mark ownership-repair failures as
   `needs_human`.
5. Update the merge critical section to perform a final operator-state refresh
   immediately before merge attempt creation and `merge_pr()`, re-running
   decision/grace/settle checks only if that refresh changed runtime state.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`

Pass criteria: the focused operator-hint monitor tests pass. Full AWF/GitHub
validation remains managed by AWF after agent completion.
