# Operator Hint Terminal Stale Persist Plan

## Problem Statement

An older PR monitor loop can load a pending operator hint, while a concurrent loop
persists the same `operation_id` as terminal (`needs_human` or `agent_failed`).
When the stale loop later persists its state, it serializes the pending hint and
the concurrent merge treats equal operation IDs as a full match, leaving the
terminal DB status overwritten by `pending`.

## Scope

- Preserve terminal operator hint status already stored in the workspace row.
- Keep processed markers and unrelated thread/freeze state behavior unchanged.
- Add a focused regression test for the same-operation stale persist race.

## Requirements

- [x] A stale pending hint with the same operation ID must not overwrite a DB
      terminal hint status.
- [x] The persisted hint should retain the DB terminal status reason.
- [x] Existing processed-marker behavior must keep clearing already processed
      hints.
- [x] Verification must use focused tests only; full AWF/GitHub validation is
      managed after agent completion.

## Implementation Steps

1. Add a failing unit regression in `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
2. Update the concurrent operator hint merge helper in
   `src/awf/runtime/pr_monitor_runner/lifecycle.py`.
3. Run the targeted operator-hint test file, or a narrower node first if useful.
4. Record validation evidence in a matching validation document.
