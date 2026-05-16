# Request Admission Rate Limiting Validation

Plan reference: `plans/REQUEST_ADMISSION_RATE_LIMITING_PLAN.md`

Source contract: `docs/awf-plans/ws_8b76839898f1400abc16ad08.md`

## Requirement Status

- Complete: Bound fresh v1 workspace creation requests with a per-identity fixed-window limiter.
- Complete: Bound fresh v2 workspace creation requests before disk admission and workspace row creation.
- Complete: Bound fresh callback registration requests before new subscription creation.
- Complete: Prefer sanitized bearer-token identity when a non-empty `Authorization: Bearer` header is present.
- Complete: Use a client-host fallback identity scoped by endpoint family when bearer identity is unavailable.
- Complete: Preserve cheap idempotency replay for identical existing workspace and callback idempotency keys while limiting fresh keys.
- Complete: Return structured 429 `ErrorResponse` payloads with `WORKSPACE_CREATE_RATE_LIMITED` and `CALLBACK_REGISTER_RATE_LIMITED` reason codes plus limiter metadata.
- Complete: Keep raw bearer tokens and raw `Authorization` headers out of limiter metadata and response payloads.
- Complete: Add minimal config knobs with permissive local defaults.
- Complete: Leave auth posture, callback target policy, and scheduler/resource-capacity behavior unchanged.

## Evidence

Files changed:

- `src/awf/api/request_admission.py`
- `src/awf/api/routes/workspaces.py`
- `src/awf/api/routes/callbacks.py`
- `src/awf/service/callbacks.py`
- `src/awf/common/config.py`
- `tests/unit/api/test_deps.py`
- `tests/unit/api/test_workspaces.py`
- `tests/unit/api/test_callbacks.py`
- `openapi.json`
- `plans/REQUEST_ADMISSION_RATE_LIMITING_PLAN.md`

TDD failure observed before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/api/test_deps.py -q
```

Failed at collection with `ModuleNotFoundError: No module named 'awf.api.request_admission'`.

Final validation commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/api/test_deps.py -q
# 199 passed in 210.13s

uv run --python 3.12 --extra dev ruff check src/awf tests
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 156 source files

uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
# OK: openapi.json matches the current app spec.
```

## Gaps

None.
