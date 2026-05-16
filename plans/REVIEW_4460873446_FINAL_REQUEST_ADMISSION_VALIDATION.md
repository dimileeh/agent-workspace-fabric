# Review 4460873446 Final Request Admission Validation

Plan reference:
`plans/REVIEW_4460873446_FINAL_REQUEST_ADMISSION_PLAN.md`

## Requirement Status

- Complete: `request=None` direct admission uses a shared process-local limiter
  so repeated no-request calls can exhaust quota.
  - Evidence: `src/awf/api/request_admission.py` now returns
    `_NULL_REQUEST_LIMITER` for `request is None`.
  - Test evidence:
    `tests/unit/api/test_deps.py::test_request_admission_none_request_uses_shared_direct_limiter`.

- Complete: Workspace create v1 and v2 run a non-consuming admission preview
  before the durable replay lookup for replay-key-cache misses.
  - Evidence: `src/awf/api/routes/workspaces.py` now calls
    `check_request_async()` before `_workspace_create_v1_durable_replay_response`
    and `_workspace_create_v2_durable_replay_response` on cache misses.
  - Test evidence:
    `test_rate_limit_checks_fresh_idempotency_key_before_exact_durable_replay_miss`
    asserts the preview happens before the exact advisory lock+lookup for both
    v1 and v2.

- Complete: Cold persisted idempotency replays still return the original
  accepted response and do not consume fresh quota.
  - Evidence: durable replay lookup still occurs before the consuming
    `admit_request_async()` call, and the broader workspace API tests pass,
    including existing cold-cache replay coverage.

- Complete: If final admission denies an idempotency-keyed workspace create
  after a durable miss, the handler performs one more durable replay lookup
  before returning `429`.
  - Evidence:
    `test_rate_limited_workspace_create_uses_post_denial_durable_replay`
    verifies v1 and v2 return the replayed accepted response when the second
    durable lookup finds the workspace after admission denial.

- Complete: Existing known replay-key, conflict, replay-unavailable,
  rate-limit payload, and v2 disk-admission behavior remain intact.
  - Evidence: the touched API modules passed together after implementation.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_none_request_uses_shared_direct_limiter -q
```

Before implementation: failed as expected because the second no-request
admission was still allowed. After implementation: passed, `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_checks_fresh_idempotency_key_before_exact_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_uses_post_denial_durable_replay -q
```

Before implementation: failed as expected, `4 failed`, because cache misses
locked before any preview and denied creates did not perform a second durable
replay. After implementation: passed, `4 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q
```

Result: passed, `266 passed in 86.76s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py
git diff --check
```

Result: all passed.

## Gaps

No remaining gaps.
