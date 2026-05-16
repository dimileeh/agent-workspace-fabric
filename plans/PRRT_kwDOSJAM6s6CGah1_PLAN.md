# PRRT_kwDOSJAM6s6CGah1 Plan

## Problem Statement and Scope

The runtime-stranding failure path in `src/awf/control/worker.py` rechecks stale
claims in Python, then calls `WorkspaceRepository.transition()` on the loaded
row. On non-Postgres dialects `get_for_update()` does not provide a row lock, so
a concurrent worker can refresh or clear the relevant claim between the Python
check and the transition. This plan addresses only the PR review thread
`PRRT_kwDOSJAM6s6CGah1`.

## Requirements Checklist

- Add regression coverage proving `_fail_stranded_workspace()` does not fail a
  workspace if the relevant claim is refreshed before the terminal transition.
- Keep existing failure-causality preservation behavior intact.
- Guard runtime-stranding failure transitions with atomic status and claim
  predicates by using `transition_if_current()`.
- Preserve behavior for statuses without execution or monitor claims.
- Run a focused unit test that covers the new regression.

## Implementation Steps

1. Add a worker unit test analogous to the stale-active-execution refreshed-claim
   regression, but targeting `_fail_stranded_workspace()`.
2. Add a small helper that maps workspace status to the correct claim predicate
   tuple for guarded transitions.
3. Change `_fail_stranded_workspace()` to reuse the Python claim cutoff and call
   `transition_if_current(..., extra_conditions=...)`, returning early if the
   guarded transition misses.
4. Run the focused worker unit tests that cover the new and adjacent behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "runtime_stranding_failure_transition_rechecks_refreshed_claim or stale_active_execution_failure_transition_rechecks_refreshed_claim"`
  must pass.
