# Operator Metadata Auth Validation

Plan reference: `plans/OPERATOR_METADATA_AUTH_PLAN.md`

## Requirement Status

- Complete: Confirm current tests fail for at least one unprotected documented
  operator metadata route.
  - Evidence: after expanding the contract registry/metadata regression,
    `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py -q`
    failed with missing `require_api_token` dependencies for the reported
    routes.
- Complete: Add `require_api_token` to tasks, merge queue, locks, metrics, and
  `/release-readiness` route metadata as needed.
  - Evidence: updated `src/awf/api/routes/tasks.py`,
    `src/awf/api/routes/merge_queue.py`, `src/awf/api/routes/locks.py`,
    `src/awf/api/routes/metrics.py`, and `src/awf/api/routes/health.py`.
- Complete: Keep `/healthz` and `/readyz` explicitly public.
  - Evidence: `tests/unit/contracts/test_surface_metadata_alignment.py`
    continues to assert those routes do not include `require_api_token`.
- Complete: Preserve existing response schemas and route behavior for
  authenticated callers.
  - Evidence: full API route suite passed with authenticated test clients.
- Complete: Validate with narrow contract/API tests covering auth metadata and
  unauthorized requests.
  - Evidence: contract and API suites passed after the implementation.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api -q`
  - Passed: 685 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts -q`
  - Passed: 497 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed.

## Notes

- `python scripts/generate_openapi.py --check` failed outside the `uv` managed
  environment because `fastapi` was not importable. The same check passed
  through `uv`, which is the environment used by the repo validation commands.
- No remaining gaps.
