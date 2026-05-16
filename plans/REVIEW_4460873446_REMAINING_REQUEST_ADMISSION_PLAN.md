# Review 4460873446 Remaining Request Admission Plan

## Problem Statement And Scope

PR review-level comment `issue:4460873446` still reports three request-admission
and idempotency-cache concerns after earlier fixes:

- Workspace create idempotency replay-key caches are constructed without the
  configured max-entry bound.
- Async route handlers call the limiter's synchronous, lock-protected
  admission method directly on the event loop.
- `admit_request(None, ...)` intentionally uses a fresh direct limiter, but that
  bypass is silent if it appears unexpectedly.

Scope is limited to request admission helper behavior, workspace/callback route
integration, focused regression tests, and this plan/validation record.

## Requirements Checklist

- [ ] Add a regression proving workspace create replay-key caches created
  through app state evict older keys at
  `_WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES`.
- [ ] Wire workspace create replay-key cache construction through a bounded
  factory while preserving the explicit unbounded class default.
- [ ] Add an async request-admission helper that keeps identity/app-state work on
  the caller thread but runs the limiter's lock-protected `admit()` call in a
  worker thread.
- [ ] Update workspace v1/v2 and callback registration routes to use the async
  helper.
- [ ] Add a structured warning for `request=None` direct limiter creation so the
  intentional no-request bypass is operator-visible.
- [ ] Preserve existing idempotency replay, conflict, rate-limit metadata, and
  direct-test-object behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/api/test_workspaces.py` and
   `tests/unit/api/test_deps.py` for the bounded workspace cache, async
   admission helper, and `request=None` warning.
2. Run the focused new tests before implementation and record the expected
   failures.
3. Add a bounded workspace replay-key cache factory and use it for app-state,
   direct request-local, and `None` construction paths.
4. Add `admit_request_async()` in `src/awf/api/request_admission.py`, route the
   lock-protected limiter call through `asyncio.to_thread()`, and update route
   handlers to await it.
5. Add the `request_admission.no_request_bypassing_limiter` warning in the
   `request=None` direct path.
6. Run the focused regressions, touched API unit modules, lint, type checking,
   and `git diff --check`.
7. Record requirement-by-requirement validation in
   `plans/REVIEW_4460873446_REMAINING_REQUEST_ADMISSION_VALIDATION.md`.

## Verification Commands And Pass Criteria

Pass criteria: new regressions fail before implementation and pass after; the
relevant API unit modules remain green; lint, type checking, and whitespace
checks pass.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_app_state_is_bounded -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_admit_request_async_uses_worker_thread_for_limiter_admission tests/unit/api/test_deps.py::test_request_admission_none_request_logs_limiter_bypass -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py
git diff --check
```
