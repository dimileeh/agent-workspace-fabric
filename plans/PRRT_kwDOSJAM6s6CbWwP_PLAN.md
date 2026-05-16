# PRRT_kwDOSJAM6s6CbWwP Callback Replay Admission Plan

## Problem Statement

The callback registration route currently checks durable idempotency replay before request admission. After a caller exhausts `callback_register_rate_limit_count`, fresh `Idempotency-Key` values still perform a database replay lookup before the route returns HTTP 429. The review asks to move fresh-key durable replay misses behind the limiter while preserving cheap known replay behavior.

## Requirements Checklist

- [ ] Keep callbacks disabled, missing/invalid `Idempotency-Key`, and in-memory response replay behavior unchanged.
- [ ] Ensure unknown fresh keys that are rejected by callback registration admission do not call `CallbackService.replay_existing`.
- [ ] Preserve idempotent replay for keys already accepted by this process, including when the full response replay cache has been replaced.
- [ ] Preserve durable replay/conflict behavior for admitted requests whose key is not in the process-local replay cache.
- [ ] Add or update regression tests for the reviewed ordering.
- [ ] Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Update the existing callback admission regression so it expects the rejected fresh key to be rate-limited before durable replay.
2. Add a small process-local positive replay-key cache keyed by `Idempotency-Key` and request hash.
3. In `register_callback`, check the full response replay cache first; use the positive key cache only to permit known durable replay before admission.
4. Run admission before durable replay for unknown keys; if admitted, keep the existing durable replay/register path.
5. Remember keys in the positive key cache whenever durable replay or registration returns a valid response.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`
