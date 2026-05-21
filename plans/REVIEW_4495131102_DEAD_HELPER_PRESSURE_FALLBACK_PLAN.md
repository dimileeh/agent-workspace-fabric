# Review 4495131102 Dead Helper and Pressure Fallback Plan

## Problem Statement and Scope

Review-level comment `issue:4495131102` flagged two follow-up issues in the
local capacity scheduler PR:

- `local_capacity_blocked_condition` is exported and tested but unused in
  production, and it omits the unsatisfiable classification handled by
  `local_capacity_blocker`.
- The console capacity panel no longer displays full reserved
  `capacity.pressure_reasons` when queue blockers and allocated-capacity
  pressure are absent.

Scope is limited to the shared capacity helper module, its focused unit tests,
the console capacity panel, console source regression coverage, and this
plan/validation pair. No branch changes, pushes, GitHub writes, or unrelated
refactors.

## Requirements Checklist

- Remove the unused `local_capacity_blocked_condition` helper so future callers
  cannot use a predicate that skips `unsatisfiable` classification.
- Keep `local_capacity_blocker` as the single production helper for local
  capacity gate decisions.
- Preserve console queue blocker badges as the highest-priority capacity
  pressure display.
- Preserve allocated-capacity pressure reasons as the second display branch.
- Add full reserved `capacity.pressure_reasons` as the tertiary fallback when
  queue blockers and allocated-capacity pressure are empty.
- Run focused Python and console validation commands.

## Implementation Steps

1. Add regressions for the removed helper export and the console pressure
   fallback display path, then confirm they fail where practical.
2. Remove `local_capacity_blocked_condition` from
   `src/awf/service/resource_capacity.py` and update focused tests to cover
   the remaining blocker behavior.
3. Update `ResourceCapacityPanel` to compute queue blocker entries and pressure
   fallback reasons once, then render the tertiary full-capacity fallback.
4. Run targeted unit/source tests plus focused lint/type validation for changed
   files.
5. Record results in
   `plans/REVIEW_4495131102_DEAD_HELPER_PRESSURE_FALLBACK_VALIDATION.md`.

## Assumptions/Changes

- The workspace `.venv` is root-owned, so Python validation may set
  `UV_PROJECT_ENVIRONMENT=/tmp/awf-review-4495131102-venv` while still using
  the same `uv run --python 3.12 --extra dev ...` commands.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q`
  passes.
- `npm --prefix apps/console run test -- lib/console-dashboard-source.test.mjs`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/resource_capacity.py tests/unit/service/test_resource_capacity.py`
  passes.
- `npm --prefix apps/console run lint -- components/console-dashboard.tsx lib/console-dashboard-source.test.mjs`
  passes.
- `npm --prefix apps/console run typecheck` passes.
