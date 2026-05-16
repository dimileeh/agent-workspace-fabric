# Review 4460873446 Final Request Admission Plan

## Problem Statement And Scope

Review-level comment `issue:4460873446` still reports three request-admission
edge cases:

- `admit_request(None, ...)` receives a new direct limiter on every call, so
  direct no-request call sites cannot actually be rate limited.
- Workspace create handlers return `429` immediately after an admission denial,
  without one more durable idempotency replay lookup for the race where the
  original keyed create becomes visible after the first lookup missed.
- Workspace create handlers acquire the idempotency advisory lock before any
  admission gate for replay-key-cache misses.

Scope is limited to the request admission helper, workspace create v1/v2
ordering, focused API regressions, and this plan/validation record.

## Requirements Checklist

- [ ] `request=None` direct admission must use a shared process-local limiter so
  repeated no-request calls can exhaust quota, while preserving warning coverage
  and request-local behavior for non-Starlette test objects.
- [ ] Workspace create v1 and v2 must run a non-consuming admission preview
  before the durable replay lookup for replay-key-cache misses.
- [ ] Cold persisted idempotency replays must still return the original
  accepted response and must not consume fresh quota.
- [ ] If final admission denies an idempotency-keyed workspace create after a
  durable miss, the handler must perform one more durable replay lookup before
  returning `429`.
- [ ] Existing known replay-key, conflict, replay-unavailable, rate-limit
  payload, and v2 disk-admission behavior must remain intact.

## Implementation Steps

1. Update `tests/unit/api/test_deps.py` so the no-request direct limiter
   regression expects the second call with the same endpoint/identity to be
   denied.
2. Update workspace create regressions to prove the non-consuming admission
   preview runs before the exact durable lock+lookup on replay-key-cache misses.
3. Add a workspace create regression for the post-denial second-chance durable
   replay path, parameterized over v1 and v2.
4. Run focused tests before implementation and record the expected failures.
5. Add a shared no-request direct limiter in `src/awf/api/request_admission.py`.
6. Import and use `check_request_async` in `src/awf/api/routes/workspaces.py`;
   preview admission before durable replay for cache misses, and perform a
   post-denial durable replay before emitting a structured `429`.
7. Run focused tests, touched API modules as justified, lint/type checks, and
   `git diff --check`.
8. Record requirement-by-requirement validation in
   `plans/REVIEW_4460873446_FINAL_REQUEST_ADMISSION_VALIDATION.md`.

## Verification Commands And Pass Criteria

Pass criteria: focused regressions fail before implementation and pass after;
workspace replays preserve `202` semantics under exhausted quota; fresh keys
remain bounded with structured `429`; lint, type checking, and whitespace
checks pass.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_none_request_uses_shared_direct_limiter -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_checks_fresh_idempotency_key_before_exact_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_uses_post_denial_durable_replay -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py -q
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py
git diff --check
```
