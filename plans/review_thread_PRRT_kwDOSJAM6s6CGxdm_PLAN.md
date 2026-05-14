# Review Thread PRRT_kwDOSJAM6s6CGxdm Plan

## Problem Statement and Scope

The failure-causality reset query detects `workspace.remonitor_requested`
epoch resets only when the event payload contains `state_reset.to`. A
remonitor event can also record the reset target directly in `new_state`, and
that signal should prevent pre-remonitor primary failure evidence from being
restored into a later failure epoch.

Scope is limited to `src/awf/service/failure_causality.py`, its unit
regression coverage, and this plan/validation record.

## Requirements Checklist

- Add a regression test showing that a remonitor event with
  `new_state="monitoring_pr"` and no `state_reset` payload resets the failure
  epoch.
- Preserve existing support for remonitor events whose reset target exists only
  in `payload["state_reset"]["to"]`.
- Keep reset ordering semantics unchanged.
- Run the narrow unit test that proves the regression and nearby protected
  behavior.
- Commit the local fix without pushing or switching branches.

## Implementation Steps

1. Add a failing unit regression in `tests/unit/service/test_failure_causality.py`
   for the pre-remonitor primary failure bleed described by the review thread.
2. Update `_failure_epoch_reset_conditions` so `workspace.remonitor_requested`
   events reset the epoch when either `new_state` is a reset state or
   `payload.state_reset.to` is a reset state.
3. Run the targeted failure-causality unit tests.
4. Create `plans/review_thread_PRRT_kwDOSJAM6s6CGxdm_VALIDATION.md` with
   requirement-by-requirement evidence.
5. Stage only touched files and commit with a thread-specific conventional
   commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`

Pass criteria: the new regression and existing failure-causality tests pass.
