# Stale Embedded Primary Snapshot Plan

## Problem Statement and Scope

An old failed-state event can contain an embedded `primary_failure` payload after a failed workspace is resumed and its live `failure_reason` and `failure_message` fields are cleared. `load_primary_failure_snapshot` currently treats that embedded payload as sufficient workspace evidence, so later stale-execution or runtime-stranding failures may skip assigning a fresh failure reason.

Scope is limited to failure-causality snapshot loading and a focused regression test. Caller behavior should remain unchanged when the workspace still has live failure evidence.

## Requirements Checklist

- Add a regression test proving a cleared/resumed workspace does not bootstrap a primary snapshot from stale embedded event payload alone.
- Keep embedded `primary_failure` enrichment when at least one live workspace failure field is still present.
- Preserve validation-run attachment behavior for live validation failures.
- Run the narrow unit test target that covers `failure_causality`.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_failure_causality.py` for a workspace whose old failed event has `primary_failure`, then whose live failure fields are cleared.
2. Update `load_primary_failure_snapshot` so embedded event data can enrich live workspace evidence but cannot satisfy workspace evidence by itself.
3. Run the focused test file and any necessary narrower command while iterating.
4. Create a validation document with requirement status and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`

Pass criteria: the regression fails before the implementation change when practical, then the full focused test file passes after the fix.
