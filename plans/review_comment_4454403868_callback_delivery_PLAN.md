# Review Comment 4454403868 Callback Delivery Plan

## Problem Statement and Scope

PR #249 received a review-level callback-delivery hardening comment. The current
branch already validates every resolved callback address and delivery can fall
back across the validated addresses, so the stale "pins to addresses[0]" premise
must not remove that safety. The actionable scope is to make dual-stack pinning
more predictable, add observability for delivery-time target rejection, and make
HTTP pinned-request extension handling explicit.

## Requirements Checklist

- Preserve validation of all resolved callback target addresses before delivery.
- Preserve fallback across multiple validated callback target addresses.
- Prefer IPv4 addresses before IPv6 addresses when both families resolve.
- Emit a structured warning log when delivery-time target validation rejects a
  callback target with `CALLBACK_TARGET_INVALID`.
- Keep target-invalid failures retryable through the existing repository path.
- Make pinned HTTP delivery explicitly pass no httpx extensions.
- Add focused regression tests for the changed behavior.
- Do not push, switch branches, or write any GitHub comment.

## Implementation Steps

1. Add failing service tests for IPv4-preferred ordering while retaining fallback,
   target-invalid structured logging, and pinned HTTP extension handling.
2. Update `src/awf/service/callbacks.py` to log target-invalid validation
   failures, order validated DNS addresses by family, and explicitly set
   `extensions = None` for pinned HTTP requests.
3. Run the narrow callback service test subset, then run relevant lint/type
   checks if time permits.
4. Record validation evidence in the matching validation document.
5. Stage only changed files and commit locally with the requested review-comment
   message format.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if run; otherwise document why it was skipped.
