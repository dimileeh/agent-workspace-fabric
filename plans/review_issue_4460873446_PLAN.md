# Review Issue 4460873446 Plan

## Problem Statement and Scope

Address PR review-level comment `issue:4460873446` covering two request-admission edge cases:

- A real Starlette/FastAPI `Request` without `app.state` currently falls back to a request-local limiter, which can silently disable cross-request admission accounting.
- A request marked as bearer-auth verified can silently downgrade to client-host identity if the `Authorization` header is no longer readable.

Scope is limited to request-admission behavior, focused regression tests, and this plan/validation record.

## Requirements Checklist

- Add a regression proving real `Request` objects without app state fail loudly instead of using a request-local limiter.
- Preserve direct-call compatibility for `None` and non-Starlette test objects.
- Add a regression proving verified-bearer downgrade emits a structured warning without exposing raw tokens.
- Implement the smallest request-admission change that satisfies the regressions.
- Run the focused unit tests for the touched area.

## Implementation Steps

1. Add failing tests in `tests/unit/api/test_deps.py` for the two review observations.
2. Update `src/awf/api/request_admission.py` to raise on missing app state for real Starlette `Request` instances.
3. Add structured logging for verified-bearer fallback to client-host identity.
4. Run the focused test module.
5. Record validation evidence in `plans/review_issue_4460873446_VALIDATION.md`.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q
```

Pass criteria: the focused unit module passes, including the new regressions, and no raw bearer token is logged in the downgrade path.
