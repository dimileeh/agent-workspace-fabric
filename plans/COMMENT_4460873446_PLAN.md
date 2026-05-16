# Comment 4460873446 Plan

## Problem Statement

Address the actionable review-level feedback for PR comment `issue:4460873446` without changing AWF branch or push state. The feedback covers request-admission rate-limit accuracy, callback idempotency replay efficiency, production guardrail signature clarity, and direct-call limiter isolation.

## Scope

- Refresh workspace-create 429 decisions after durable idempotency replay probes so `Retry-After` reflects the current limiter window.
- Consolidate fresh callback registration idempotency replay and create work so the hot path does not acquire the same advisory lock in three separate sessions.
- Remove the unused `callbacks_enabled` argument from `settings_guardrails` and update call sites/tests.
- Stop sharing limiter state across `request=None` callers.
- Add or update focused regression tests for the behavior changes.

## Requirements Checklist

- [ ] Workspace v1/v2 idempotency-key 429 responses use a fresh request-admission decision after durable replay miss.
- [ ] Callback fresh registration path keeps cold durable replay bypass semantics while avoiding repeated advisory-lock acquisition for the same fresh key.
- [ ] `settings_guardrails` has no silently discarded `callbacks_enabled` parameter and callers compile against the new signature.
- [ ] `admit_request(None, ...)` callers do not share limiter buckets across calls.
- [ ] Narrow tests for touched API/config/request-admission behavior pass.

## Implementation Steps

1. Write failing regression tests for stale workspace retry-after refresh, callback fresh-path lock consolidation, `settings_guardrails` signature clarity, and `None` request limiter isolation.
2. Update request admission direct limiter behavior for `None` requests.
3. Update workspace create v1/v2 to re-check admission after a denied preview and durable replay miss.
4. Refactor callback service/route/repository helpers to reuse a single locked session for fresh registration.
5. Remove `callbacks_enabled` from `settings_guardrails` and update call sites.
6. Run focused tests, then broader unit/API tests if the touched area warrants it.
7. Write validation notes and commit the changes locally.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/service/test_config.py tests/unit/service/test_callbacks.py tests/unit/db/test_callback_repository.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py src/awf/common/config.py src/awf/service/callbacks.py src/awf/db/repositories.py tests/unit/api/test_deps.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py tests/unit/service/test_config.py`

Pass criteria: all focused tests and lint checks complete successfully, or any environmental blocker is documented in the validation file.
