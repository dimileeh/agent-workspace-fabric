# Monitor Reattach Restart Plan

## Problem Statement

After a local AWF worker restart, workspace `ws_cdd335704e12498ca87be8d4`
was marked `failed` while it was monitoring PR #280. The workspace had an open
PR URL and was safely recoverable by remonitoring, but the stale-active-execution
scanner treated its still-running compose runtime as an unrecoverable lost
execution.

## Scope

- Fix restart recovery for `monitoring_pr` workspaces with open PRs.
- Keep active execution failure behavior unchanged for `running`, `validating`,
  and `pushing` workspaces.
- Do not change PR monitor decision policy or merge policy.
- Recover the affected workspace with the existing remonitor control.

## Requirements

- A `monitoring_pr` workspace with an open PR and an expired monitor claim must
  be recoverable even when its old compose runtime still reports `running`.
- The worker must clear stale claims and redispatch the PR monitor instead of
  transitioning the workspace to `failed`.
- Add regression coverage for the running-runtime case, including the case where
  a prior stale-active-execution event already exists.
- Validate with focused worker tests.

## Implementation Steps

1. Add a worker regression test for a running `monitoring_pr` runtime after
   restart with a stale monitor claim and an existing stale-active event.
2. Update stale-active recovery to classify open-PR monitors as remonitorable
   before the generic running-runtime failure branch.
3. Run focused tests for the touched worker behavior.
4. Record validation evidence.

## Pass Criteria

- The regression test fails on the old behavior and passes after the fix.
- The workspace remains `monitoring_pr`, stale claims are cleared/reclaimed, and
  the executor resumes the PR monitor.
- `ws_cdd335704e12498ca87be8d4` is manually recovered to monitor PR #280.
