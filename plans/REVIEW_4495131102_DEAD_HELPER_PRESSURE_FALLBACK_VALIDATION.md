# Review 4495131102 Dead Helper and Pressure Fallback Validation

Plan reference:
`plans/REVIEW_4495131102_DEAD_HELPER_PRESSURE_FALLBACK_PLAN.md`

## Requirement Status

- Complete: Removed `local_capacity_blocked_condition` from
  `src/awf/service/resource_capacity.py`.
- Complete: Preserved `local_capacity_blocker` as the shared helper that
  classifies both deferred and unsatisfiable capacity decisions.
- Complete: Preserved console queue blocker badges as the first pressure
  display branch.
- Complete: Preserved allocated-capacity pressure reasons as the second display
  branch.
- Complete: Added full reserved `capacity.pressure_reasons` as the tertiary
  pressure badge fallback.
- Complete: Ran focused Python and console validation commands.

## Evidence

Files changed:

- `src/awf/service/resource_capacity.py`
- `tests/unit/service/test_resource_capacity.py`
- `apps/console/components/console-dashboard.tsx`
- `apps/console/lib/console-dashboard-source.test.mjs`
- `plans/REVIEW_4495131102_DEAD_HELPER_PRESSURE_FALLBACK_PLAN.md`
- `plans/REVIEW_4495131102_DEAD_HELPER_PRESSURE_FALLBACK_VALIDATION.md`

TDD failure evidence:

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/service/test_resource_capacity.py -q`
  - Failed as expected before implementation:
    `test_local_capacity_module_does_not_export_incomplete_blocked_predicate`.
- `npm --prefix apps/console run test -- lib/console-dashboard-source.test.mjs`
  - Failed as expected before implementation:
    `capacity panel falls back to full reserved pressure reasons`.

Commands run after implementation:

- `UV_PROJECT_ENVIRONMENT=/tmp/awf-review-4495131102-venv uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py -q`
  - Passed: `7 passed`.
- `npm --prefix apps/console run test -- lib/console-dashboard-source.test.mjs`
  - Passed: `99 passed`.
- `UV_PROJECT_ENVIRONMENT=/tmp/awf-review-4495131102-venv uv run --python 3.12 --extra dev ruff check src/awf/service/resource_capacity.py tests/unit/service/test_resource_capacity.py`
  - Passed.
- `UV_PROJECT_ENVIRONMENT=/tmp/awf-review-4495131102-venv uv run --python 3.12 --extra dev mypy src/awf/service/resource_capacity.py`
  - Passed.
- `npm --prefix apps/console run lint -- components/console-dashboard.tsx lib/console-dashboard-source.test.mjs`
  - Passed.
- `npm --prefix apps/console run typecheck`
  - Passed.

## Remaining Gaps

None.
