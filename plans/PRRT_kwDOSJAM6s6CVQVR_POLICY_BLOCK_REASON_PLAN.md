# PRRT_kwDOSJAM6s6CVQVR Policy Block Reason Plan

## Problem Statement And Scope

The CI-repair path in `src/awf/runtime/pr_monitor_runner.py` catches
`_MonitorPolicyBlockedError` from `_commit_dirty_worktree()` but returns a
`_GitPushResult` with the default `GIT_PUSH_FAILED` reason. This loses the
policy-block provenance that adjacent monitor repair paths preserve.

Scope is limited to the CI-repair commit path referenced by review thread
`PRRT_kwDOSJAM6s6CVQVR`.

## Requirements Checklist

- Add a regression test proving a CI-repair policy block returns
  `MONITOR_POLICY_BLOCKED`.
- Keep existing protected-scope and generic git-push semantics unchanged.
- Make the smallest code change needed to preserve the policy-block reason.
- Run focused tests covering the new regression and nearby monitor behavior.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add a focused unit test that exercises `_run_ci_fix()` when
   `_commit_dirty_worktree()` raises `_MonitorPolicyBlockedError`.
2. Confirm the new test fails because the result reason remains
   `GIT_PUSH_FAILED`.
3. Set `reason_code=_MONITOR_POLICY_BLOCKED_REASON` in that handler.
4. Re-run the focused test file or targeted tests.
5. Save validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`

Pass criteria: the new regression and existing monitor coverage-edge tests pass.

## Assumptions/Changes

- The full coverage-edge file currently fails in unrelated protected-scope
  repair cases. This thread-specific fix remains scoped to CI-repair
  policy-block provenance, so validation also includes targeted policy-block
  tests that cover the changed behavior directly.
