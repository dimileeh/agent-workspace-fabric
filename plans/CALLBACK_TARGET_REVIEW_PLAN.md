# Callback Target Review Fix Plan

## Problem Statement And Scope

Address PR review comment `issue:4454403868` for callback target hardening.
The reviewed gaps are:

- Delivery-time callback target validation timeouts are recorded as
  `CALLBACK_REQUEST_FAILED` instead of `CALLBACK_TARGET_INVALID`.
- 6to4 IPv6 addresses in `2002::/16` can pass public-host validation even when
  they can route to embedded private IPv4 space.

Scope is limited to callback target validation and focused regression tests.

## Requirements Checklist

- [ ] Classify callback target validation timeouts as
  `CALLBACK_TARGET_INVALID`.
- [ ] Reject 6to4 callback target host literals at registration-time host
  validation.
- [ ] Reject 6to4 addresses returned by delivery-time DNS resolution.
- [ ] Preserve existing handling for ordinary public hosts, IPv4-mapped IPv6,
  NAT64, and request/post failures.
- [ ] Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Update existing timeout regression to expect `CALLBACK_TARGET_INVALID` and
   target-invalid logging.
2. Add 6to4 regression coverage for shared host policy and delivery-time DNS
   resolution.
3. Update callback validation code to route validation timeout as a validation
   rejection and block `2002::/16` in both host-literal and resolved-IP checks.
4. Run focused tests, then narrow lint/type checks if needed for changed files.
5. Create `plans/CALLBACK_TARGET_REVIEW_VALIDATION.md` with plan evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py src/awf/service/callbacks.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py`
  must pass.
