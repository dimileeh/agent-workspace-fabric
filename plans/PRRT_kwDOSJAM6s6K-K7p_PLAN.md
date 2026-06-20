# PRRT_kwDOSJAM6s6K-K7p Plan

## Problem Statement and Scope

The PR monitor missing-HEAD recovery path can create a recovered commit before
pre-push validation. If agent-runtime ownership repair then fails, the current
failure return may leave the worktree advanced to that unvalidated recovered
commit instead of restoring the saved recovery head.

Scope is limited to the recovered-head ownership-repair failure branch in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and focused
regression coverage for that branch.

## Requirements Checklist

- Add a regression test proving recovered-head ownership repair failure invokes
  pre-push validation cleanup with `restore_ref` set to the original recovery
  head.
- Preserve the existing failure reason and message for ownership repair failure.
- Keep the implementation minimal and aligned with neighboring recovered-head
  failure cleanup paths.
- Run only focused validation for the changed behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a focused unit test in the existing recovered-head edge test module.
2. Run that test and confirm it fails before the implementation change when
   practical.
3. Add cleanup before the ownership-repair failure return.
4. Re-run the focused test.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges_part_002.py -q`

Pass criteria: the focused test module passes, and no broad AWF/GitHub-owned
validation is run locally.
