# Review 4454403868 Summary Nits Plan

## Problem Statement and Scope

Address the two review-level callback comments from PR #249:

- Remove the unreachable outer `CallbackTargetValidationTimeoutError` handler in callback delivery.
- Keep callback registration error codes consistent with delivery by reporting configurable policy rejections as `CALLBACK_TARGET_POLICY_VIOLATION`.

Scope is limited to callback registration/delivery handling, focused regression coverage, and validation artifacts for this review comment.

## Requirements Checklist

- [ ] Registration rejects HTTPS-only policy violations with `CALLBACK_TARGET_POLICY_VIOLATION`.
- [ ] Registration rejects allowlist policy violations with `CALLBACK_TARGET_POLICY_VIOLATION`.
- [ ] Other callback target policy errors continue to report `CALLBACK_TARGET_INVALID`.
- [ ] Delivery loop no longer contains the misleading unreachable outer timeout handler.
- [ ] Focused tests pass.

## Implementation Steps

1. Update the existing registration policy tests to expect `CALLBACK_TARGET_POLICY_VIOLATION` and confirm they fail before the route change.
2. Import and catch `CallbackTargetPolicyViolationError` before the broader `CallbackTargetPolicyError` catch in the registration route.
3. Remove the unreachable outer `except CallbackTargetValidationTimeoutError` block in the delivery loop.
4. Run focused API/service callback tests.
5. Create the validation document with requirement-by-requirement status and evidence.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q
```

Pass criteria: both commands pass, and the changed tests fail against the pre-fix route behavior.
