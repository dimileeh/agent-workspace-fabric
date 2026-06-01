# PRRT_kwDOSJAM6s6GOim1 Recheck Prelaunch Plan

## Problem Statement And Scope

The provisioner pre-publishes `compose_project_name` after host-port checks and
before `_recheck_before_launch()`. If `_recheck_before_launch()` raises, no
Compose stack has started, but `_mark_failed(..., compose_launched=False)` leaves
the pre-published project name on the terminal failed workspace. That violates
the host-port invariant that a terminal workspace with non-null
`compose_project_name` represents a possible live runtime unless a terminal
runtime release event exists.

Scope is limited to the recheck-before-launch failure path and its regression
coverage.

## Requirements Checklist

- Add a regression test that fails before the implementation change.
- On `_recheck_before_launch()` exceptions after pre-launch metadata commit, mark
  the workspace failed without leaving `compose_project_name` set.
- Do not record `workspace.terminal_runtime_released` for this path because no
  containers were started.
- Preserve existing launched-stack failure behavior where `compose_project_name`
  remains set for cleanup.
- Run only focused local checks; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a targeted unit regression under `tests/unit/node/test_provisioner_parts`
   that raises from `_recheck_before_launch()` after the pre-launch commit.
2. Confirm the new regression fails against current code.
3. Update provisioner failure handling so unlaunched recheck failures clear the
   pre-published compose project while transitioning to failed.
4. Run the targeted regression file or individual tests needed to prove the
   change.
5. Write validation evidence to
   `plans/PRRT_kwDOSJAM6s6GOim1_RECHECK_PRELAUNCH_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py -q`

Pass criteria:

- The new regression passes.
- Existing focused provisioner regressions in that file pass.
- No broad AWF/GitHub-owned validation is run locally.
