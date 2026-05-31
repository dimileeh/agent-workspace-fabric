# PRRT_kwDOSJAM6s6F5wuq Plan

## Problem Statement and Scope

The remonitor path persists operator hints and freezes auto-merge only when
reviewer-settle elapsed state is found for `workspace.monitor_last_commit_sha`.
That workspace column can lag behind the live PR head recorded by the PR monitor
in `monitor_threads_addressed`, so a remonitor request made after settle can be
misclassified as pre-settle.

Scope is limited to remonitor past-settle detection and re-arming the existing
operator-hint freeze state.

## Requirements Checklist

- Add a regression test where `monitor_last_commit_sha` differs from the
  elapsed non-check reviewer-settle marker stored in monitor state.
- Keep existing exact-SHA behavior and warning/freeze response intact.
- When stale SHA state exists, detect the elapsed settle marker from persisted
  monitor state, persist the operator hint, emit the past-settle warning, and
  re-arm settle for the marker head SHA.
- Avoid broad AWF/GitHub validation; run only focused tests for the changed
  behavior.

## Implementation Steps

1. Add a service-level remonitor regression covering stale
   `monitor_last_commit_sha`.
2. Confirm the new regression fails against current behavior when practical.
3. Add a helper that returns the elapsed settle head SHA, preferring the
   workspace SHA when it is valid and otherwise parsing persisted settle keys.
4. Update remonitor control flow to freeze against the detected settle head SHA.
5. Run focused tests for the remonitor service behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle"`
  - Passes after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per workspace contract.
