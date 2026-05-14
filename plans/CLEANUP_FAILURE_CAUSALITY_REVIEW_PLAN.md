# Cleanup Failure Causality Review Plan

## Problem Statement And Scope

PR #242 review feedback reports that a destroy cleanup failure can be recorded
in operation/audit payloads without reaching the `workspace.state_changed`
event stream when the workspace is already `failed`. Because failure-causality
snapshots read accumulated secondary failures from failed state-change events,
the cleanup secondary can be missing from the next recovery cycle.

The same review notes that `attach_primary_failure` silently replaces an
existing `primary_failure` entry, which is a future footgun even if current
call sites pass fresh payloads.

Scope is limited to:

- `src/awf/service/controls.py` cleanup failure causality recording.
- `src/awf/service/failure_causality.py` primary-failure attachment contract.
- Focused unit tests for both behaviors.

## Requirements Checklist

- Add a regression test showing cleanup failure secondary evidence reaches a
  failed `workspace.state_changed` event even when cleanup observes an already
  `failed` workspace.
- Preserve the existing transition behavior for ordinary `destroying -> failed`
  cleanup failures.
- Do not advance cancelled/completed/destroyed stale cleanup callbacks as
  failures.
- Keep primary failure row fields and event payload reason codes rooted in the
  original primary failure when primary evidence exists.
- Make `attach_primary_failure` non-destructive or explicitly documented and
  covered by tests.
- Keep edits scoped and avoid weakening existing regression assertions.

## Implementation Steps

1. Add a focused controls lifecycle/unit regression that simulates the workspace
   reaching `failed` while destroy cleanup is running, then verifies the cleanup
   secondary appears in the latest failed state-change event and in subsequent
   `load_failure_causality_snapshot` history.
2. Add failure-causality helper coverage for the chosen `attach_primary_failure`
   contract.
3. Update `destroy_workspace` cleanup-failure handling so an already-failed
   workspace with cleanup failure continues through failure-causality recording
   and emits a same-state failed `workspace.state_changed` evidence event when
   no legal transition is available.
4. Update `attach_primary_failure` to avoid clobbering an existing
   `primary_failure` entry, preserving caller-supplied evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py -q`
  - Passes with the new regressions.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/failure_causality.py tests/unit/service/test_controls.py tests/unit/service/test_failure_causality.py`
  - No lint findings.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - No type errors in the touched service surface.
