# Review 4460873446 Request Admission Follow-ups Plan

## Problem Statement And Scope

Review-level comment `issue:4460873446` flagged two request-admission hardening
gaps in PR #256:

- `POST /v1/callbacks` checks idempotency replay before the limiter, so
  over-quota requests with fresh idempotency keys still cause a callback
  idempotency DB read.
- `request_admission_limiter()` returns a new limiter whenever a request has no
  usable `app.state`, making admission a no-op for direct or malformed request
  paths.

Scope is limited to request admission helper behavior, callback registration
ordering, focused regression tests, and the required validation notes for this
review comment.

## Requirements Checklist

- [ ] Add a regression test showing stateless request admission does not receive
  a fresh zero-count limiter on every call.
- [ ] Add a regression test showing an over-quota callback registration with a
  fresh idempotency key is rejected before `CallbackService.replay_existing`
  can issue its DB read.
- [ ] Preserve same-process callback idempotency replay bypass after quota is
  exhausted.
- [ ] Preserve idempotency conflict responses and structured 429 response
  metadata/`Retry-After` behavior.
- [ ] Keep changes scoped to the request-admission and callback registration
  surface.

## Implementation Steps

1. Add failing unit coverage in `tests/unit/api/test_deps.py` for repeated
   admission with a request-like object that has no `app.state`.
2. Add failing unit coverage in `tests/unit/api/test_callbacks.py` for
   over-quota fresh callback keys not invoking `replay_existing`.
3. Update `src/awf/api/request_admission.py` so no-state requests use shared
   limiter state instead of a disposable limiter.
4. Update `src/awf/api/routes/callbacks.py` to check a small in-memory
   idempotency replay cache first, apply request admission before fresh
   callback registration, and cache successful registration responses for
   replay bypass.
5. Run targeted tests and lint/type checks for the touched modules.

## Verification Commands And Pass Criteria

Pass criteria: the new regression tests fail before implementation and pass
after implementation; callback/workspace request admission tests remain green;
lint and mypy pass for touched files.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_reuses_limiter_without_app_state -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_replay_read -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rejects_v1_create_burst_after_configured_limit tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/callbacks.py
```
