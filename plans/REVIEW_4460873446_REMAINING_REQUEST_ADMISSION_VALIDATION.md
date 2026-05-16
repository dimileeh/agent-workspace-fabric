# Review 4460873446 Remaining Request Admission Validation

Plan reference:
`plans/REVIEW_4460873446_REMAINING_REQUEST_ADMISSION_PLAN.md`

## Requirement Status

- Complete: Add a regression proving workspace create replay-key caches created
  through app state evict older keys at
  `_WORKSPACE_CREATE_REPLAY_KEY_CACHE_MAX_ENTRIES`.
  - Evidence:
    `tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_app_state_is_bounded`.
  - Red check: failed before implementation because the oldest inserted key
    still matched after inserting `max_entries + 1` keys.
  - Green check: passed after implementation.

- Complete: Wire workspace create replay-key cache construction through a
  bounded factory while preserving the explicit unbounded class default.
  - Evidence:
    `src/awf/api/routes/workspaces.py` now creates app-state, direct
    request-local, and `None` replay-key caches through
    `_new_workspace_create_idempotency_replay_key_cache()`.
  - Existing explicit default coverage remains in
    `test_workspace_replay_key_cache_default_retains_keys_past_response_cache_limit`.

- Complete: Add an async request-admission helper that keeps identity/app-state
  work on the caller thread but runs the limiter's lock-protected `admit()` call
  in a worker thread.
  - Evidence: `src/awf/api/request_admission.py::admit_request_async`.
  - Test evidence:
    `tests/unit/api/test_deps.py::test_admit_request_async_uses_worker_thread_for_limiter_admission`.
  - Red check: failed before implementation because the async helper and
    `asyncio.to_thread` call site did not exist.

- Complete: Update workspace v1/v2 and callback registration routes to use the
  async helper.
  - Evidence:
    `src/awf/api/routes/workspaces.py` and
    `src/awf/api/routes/callbacks.py` now await `admit_request_async()`.

- Complete: Add a structured warning for `request=None` direct limiter creation
  so the intentional no-request bypass is operator-visible.
  - Evidence:
    `tests/unit/api/test_deps.py::test_request_admission_none_request_logs_limiter_bypass`.
  - Red check: failed before implementation because no warning event was
    emitted.

- Complete: Preserve existing idempotency replay, conflict, rate-limit metadata,
  and direct-test-object behavior.
  - Evidence: the touched API unit modules passed after implementation.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_app_state_is_bounded -q
```

Before implementation: failed as expected.
After implementation: passed, `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_admit_request_async_uses_worker_thread_for_limiter_admission tests/unit/api/test_deps.py::test_request_admission_none_request_logs_limiter_bypass -q
```

Before implementation: failed as expected.
After implementation: passed, `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q
```

Result: passed, `256 passed in 127.95s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py
```

Result: passed.

```bash
git diff --check
```

Result: passed.

## Gaps

None.
