# Stale Claim Atomic Transition Plan

## Problem Statement and Scope

The stale active execution failure path checks whether an execution claim is stale
in Python and then transitions the workspace to `failed`. On non-Postgres
dialects, `get_for_update()` does not serialize that check with the transition,
so a refreshed or cleared claim can race between the check and the state change.

Scope is limited to the stale active execution failure transition and the
repository transition helper behavior needed to preserve the existing failure
event payload.

## Requirements Checklist

- Add a regression test that proves a claim refresh between the stale check and
  failure transition prevents the stale failure.
- Recheck the stale execution claim with the status predicate in the atomic
  transition path.
- Preserve the existing stale failure event payload behavior when primary
  failure evidence exists.
- Keep changes scoped and avoid changing branch or PR workflow behavior.

## Implementation Steps

1. Add the regression test around `_fail_stale_active_execution()`.
2. Extend `WorkspaceRepository.transition_if_current()` to carry an optional
   event payload through both Postgres and non-Postgres paths.
3. Update `_fail_stale_active_execution()` to call `transition_if_current()` with
   a stale-claim `extra_conditions` predicate before clearing claim/failure row
   fields.
4. Run the targeted worker/repository tests, then run narrow lint/type checks if
   practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k stale_active_execution_failure`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py -q -k transition_if_current`
  passes.
- Relevant lint/type checks pass or any environment blocker is documented.
