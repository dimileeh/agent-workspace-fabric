# Callback Durable Replay Quota Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6Cfh_A` reports that a callback registration retry with an already-persisted `Idempotency-Key` can consume fresh callback registration quota when both in-memory replay caches are cold. The scope is the `POST /v1/callbacks` admission and durable replay path plus focused unit regression coverage.

## Requirements Checklist

- Add a regression test proving a cold durable callback replay does not count against `callback_register_rate_limit_count` while fresh registrations still do.
- Preserve the existing guard that fresh over-limit keys do not scan all replay keys.
- Preserve idempotency conflict behavior and replay-unavailable behavior for known replay keys.
- Keep the code change narrowly scoped to callback registration admission/replay ordering.
- Run the focused callback API test(s) that prove the fix.

## Implementation Steps

1. Add a limit-2 callback API regression: create one fresh registration, clear both replay caches, replay the same persisted key, then verify a second fresh callback is admitted and a third fresh callback is rate-limited.
2. Confirm the new regression fails against the current implementation.
3. Move cold durable replay detection before fresh quota admission without introducing an unbounded replay-key scan.
4. Re-run focused callback API tests, then create validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
- Pass criteria: the new regression and existing callback API tests pass.
