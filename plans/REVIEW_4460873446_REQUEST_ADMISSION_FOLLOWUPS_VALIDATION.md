# Review 4460873446 Request Admission Follow-ups Validation

Plan reference:
`plans/REVIEW_4460873446_REQUEST_ADMISSION_FOLLOWUPS_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing stateless request admission does not
  receive a fresh zero-count limiter on every call.
  - Evidence: `tests/unit/api/test_deps.py` adds
    `test_request_admission_reuses_limiter_without_app_state`.
  - Red check: failed before implementation because the second admission was
    still allowed.
  - Green check: targeted test passed after implementation.

- Complete: Add a regression test showing an over-quota callback registration
  with a fresh idempotency key is rejected before `CallbackService.replay_existing`
  can issue its DB read.
  - Evidence: `tests/unit/api/test_callbacks.py` adds
    `test_register_callback_rate_limit_rejects_fresh_key_before_replay_read`.
  - Red check: failed before implementation because the rejected fresh key still
    appeared in the tracked `replay_existing` calls.
  - Green check: targeted test passed after implementation.

- Complete: Preserve same-process callback idempotency replay bypass after quota
  is exhausted.
  - Evidence: `src/awf/api/routes/callbacks.py` caches successful callback
    idempotency responses on app state and checks that cache before admission.
  - Test evidence: existing callback replay/burst tests passed in the focused
    API run.

- Complete: Preserve idempotency conflict responses and structured 429 response
  metadata/`Retry-After` behavior.
  - Evidence: `tests/unit/api/test_callbacks.py` passed, including existing
    conflict and rate-limit assertions.

- Complete: Keep changes scoped to the request-admission and callback
  registration surface.
  - Evidence: code changes are limited to
    `src/awf/api/request_admission.py`, `src/awf/api/routes/callbacks.py`,
    `tests/unit/api/test_deps.py`, and `tests/unit/api/test_callbacks.py`,
    plus this plan/validation pair.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_reuses_limiter_without_app_state -q
```

Result before implementation: failed as expected.
Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_replay_read -q
```

Result before implementation: failed as expected.
Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rejects_v1_create_burst_after_configured_limit tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q
```

Result: passed, `83 passed in 61.88s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/callbacks.py tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/callbacks.py
```

Result: passed.

## Gaps

None.
