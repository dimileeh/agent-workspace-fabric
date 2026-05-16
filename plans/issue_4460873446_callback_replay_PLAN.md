# issue:4460873446 Callback Replay Plan

## Problem statement and scope

Address the review-level callback registration feedback for PR #256. The scope is
limited to the callback idempotency replay path and its tests:

- Make the persisted-key replay entry point explicit instead of a class-body
  alias.
- Preserve the current lock-and-fetch behavior used by both replay call sites.
- Document the accepted callback-route trade-off that fresh registrations can
  perform durable replay probes before the final idempotent create path, because
  those probes are required to let durable idempotency replays bypass admission
  limiting.

## Requirements checklist

- [ ] `CallbackService.replay_existing_for_persisted_key` is its own explicit
  async method.
- [ ] Existing durable replay behavior still funnels through
  `_replay_existing_locked`.
- [ ] Tests describe the explicit method contract and fail against the current
  alias implementation.
- [ ] Callback route code documents why the durable pre-admission probe is kept
  as an accepted trade-off.
- [ ] Run the narrow callback tests that prove the change.
- [ ] Create a validation document before completion.

## Implementation steps

1. Update the callback service unit test currently asserting the alias identity
   so it instead requires distinct public entry points that delegate to the same
   locked helper.
2. Run that narrow test to confirm it fails against the current implementation.
3. Replace the class-body alias with an explicit async method delegating to
   `_replay_existing_locked`.
4. Add concise route documentation around the persisted-key durable replay probe.
5. Run the narrow callback service/API tests.
6. Record validation evidence in
   `plans/issue_4460873446_callback_replay_VALIDATION.md`.
