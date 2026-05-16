# Request Admission Precheck Validation

Plan reference: `plans/request_admission_precheck_PLAN.md`

## Requirement Status

- Remove redundant `check_request_async` usage from workspace create handlers:
  Complete. `src/awf/api/routes/workspaces.py` no longer imports or calls
  `check_request_async`, and the unused helper was removed.
- Preserve rate-limited response shape and `Retry-After` behavior through the
  existing `admit_request_async` decision: Complete. The existing
  `admit_request_async` path remains the only quota decision point and still
  feeds `_workspace_create_rate_limited_response`.
- Preserve durable idempotency replay-before-rate-gate behavior for v1 and v2:
  Complete. Durable replay lookup branches remain before `admit_request_async`.
- Add/update a regression test proving fresh idempotency-key creates do not use
  the non-consuming pre-check path: Complete. The existing v1/v2 fresh-key
  rate-limit test now monkeypatches `workspaces_route.check_request_async` and
  asserts it is not called.
- Commit only the files changed for this review comment: Complete. The staged
  scope is limited to the route fix, regression test, and required plan and
  validation documents for this review comment.

## Evidence

- Confirmed the regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss -q`
  failed with `assert 2 == 0` for both v1 and v2.
- After implementation, focused regression passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss -q`
  passed with `2 passed`.
- Broader workspace API unit file passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passed with `145 passed`.
- Touched-file lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  passed.
