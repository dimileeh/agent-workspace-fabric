# Operator Hint No-Op Push Plan

## Problem Statement and Scope

An operator hint can be fully handled without producing a local fix commit, such
as when the requested work is non-code PR bookkeeping and the agent reports a
fixed verdict. In that path, a successful validated git push may be a no-op
(`pushed=False`, `failed=False`). The operator hint runner currently treats that
successful no-op as `needs_human`, which leaves auto-merge blocked even though
the hint was addressed.

Scope is limited to operator-hint repair handling, dispatcher outcome/persistence
for the no-op processed case, and focused regression tests.

## Requirements Checklist

- A fixed operator-hint verdict followed by a successful no-op push marks the
  pending operator hint processed.
- The processed marker is persisted when the dispatcher handles a successful
  no-op operator-hint repair.
- Operation logging distinguishes the processed no-op from `needs_human` and
  `agent_failed`.
- Existing failure and terminal verdict behavior remains unchanged.
- Validation stays focused; full AWF/GitHub validation remains owned by AWF
  after this agent phase.

## Implementation Steps

1. Add a failing unit regression for a fixed operator-hint verdict whose
   validated push returns `pushed=False`, asserting processed state and no human
   escalation.
2. Add a dispatcher regression for a no-op processed operator hint, asserting
   persisted processed marker and a processed operation outcome.
3. Update `src/awf/runtime/pr_monitor_runner/operator_hints.py` so successful
   no-op push results after fixed verdicts mark the hint processed.
4. Update `src/awf/runtime/pr_monitor_runner/loop.py` so no-op processed hints
   are persisted and logged separately from terminal human/agent states.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Passes after implementation.
  - Before implementation, the new regression for no-op push processing should
    fail by observing `needs_human` instead of processed.

Full repo validation, full coverage, and CI-equivalent runs are intentionally
not executed in this agent phase; AWF/GitHub own those broad gates after
completion.
