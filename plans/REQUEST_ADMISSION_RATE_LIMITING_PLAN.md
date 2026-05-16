# Request Admission Rate Limiting Plan

## Problem Statement and Scope

Add narrow request-level admission controls for expensive public-ish API endpoints:

- `POST /v1/workspaces`
- `POST /v2/workspaces`
- `POST /v1/callbacks`

The implementation follows `docs/awf-plans/ws_8b76839898f1400abc16ad08.md`.
It must bound bursts before scheduler, disk, workspace row creation, or callback
registration work, without changing the broader auth posture, callback URL
policy, scheduler capacity model, branch/PR lifecycle, or deployment topology.

## Requirements Checklist

- [ ] Bound fresh v1 workspace create requests.
- [ ] Bound fresh v2 workspace create requests before disk admission and row creation.
- [ ] Bound fresh callback registration requests before new subscription creation.
- [ ] Prefer a sanitized bearer-token identity when a non-empty `Authorization: Bearer` header is present.
- [ ] Use a safe fallback identity based on client host plus endpoint family when bearer identity is unavailable.
- [ ] Preserve cheap idempotency replay for identical existing idempotency keys while limiting bursts of fresh keys or payloads.
- [ ] Return structured 429 errors with stable reason codes and operator-visible limiter metadata.
- [ ] Avoid exposing raw `Authorization` headers or bearer tokens in limiter keys, response metadata, or log-like helper metadata.
- [ ] Keep configuration minimal, documented, dogfood-friendly, and bounded.
- [ ] Keep the slice independent from auth hardening, callback SSRF hardening, and scheduler/resource-capacity rewrites.

## Implementation Steps

1. Add failing helper tests in `tests/unit/api/test_deps.py` for bearer digesting, fallback identity, separate token buckets, endpoint family buckets, and redaction.
2. Add failing API tests in `tests/unit/api/test_workspaces.py` for v1/v2 burst rejection, v2 pre-disk rejection, idempotency replay behavior, identity separation, and structured/redacted workspace errors.
3. Add failing API tests in `tests/unit/api/test_callbacks.py` for callback burst rejection, callback idempotency replay, identity separation, and structured/redacted callback errors.
4. Implement a focused `src/awf/api/request_admission.py` fixed-window helper with a testable clock and app-state limiter accessor.
5. Add minimal settings fields in `src/awf/common/config.py` for window seconds and per-endpoint-family limits.
6. Wire admission checks into only the workspace create and callback registration routes, keeping existing idempotency conflict/replay behavior intact.
7. Add a narrow callback service/repository replay helper only if current route/service APIs cannot cheaply replay existing identical callback idempotency keys before limiter consumption.
8. Run focused tests and static checks, then fix the smallest failing surface.
9. Write `plans/REQUEST_ADMISSION_RATE_LIMITING_VALIDATION.md` with requirement status, files changed, commands run, and any gaps.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/api/test_deps.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: the focused test command, lint, and type check pass; rate-limit
rejections are structured and redacted; idempotency replay remains cheap for
already-created identical keys; and the implementation does not require auth or
change callback target policy or scheduler/resource capacity behavior.
