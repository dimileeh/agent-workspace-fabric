# PRRT_kwDOSJAM6s6KLqXS Plan

## Problem Statement and Scope

The pre-push validation path can recover a missing HEAD object by creating or
locating a replacement commit, then continue directly to validation. If that
recovered commit contains files covered by blocking supply-chain policy, the
policy refresh can be skipped because the worktree is already clean and
`_commit_dirty_worktree` is not invoked.

Scope is limited to the recovered-HEAD branch in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and focused
regression coverage for that branch.

## Requirements Checklist

- Detect changed paths introduced when missing-HEAD recovery advances from the
  recovery base to the recovered HEAD.
- Refresh supply-chain policy for those recovered paths before pre-push
  validation proceeds.
- Block pre-push validation with the existing monitor policy reason when the
  supply-chain refresh reports a blocking finding.
- Fail closed with the existing protected-scope diff-unavailable reason if the
  recovered diff cannot be calculated.
- Keep changes minimal and avoid broad AWF/GitHub validation in the agent phase.

## Implementation Steps

1. Add a focused failing unit test for recovered pre-push HEAD commits that
   trigger a blocking supply-chain refresh.
2. Add the minimal recovery-path guard in `_run_pre_push_validation`.
3. Add or adjust any focused test needed for the diff-unavailable failure path
   if existing coverage does not exercise it.
4. Run the narrow targeted tests for the changed behavior only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k recovered_head`
  should pass.
- Full AWF/GitHub validation is intentionally not run here; AWF owns broad
  validation, provenance, logs, and merge gating after this agent phase.
