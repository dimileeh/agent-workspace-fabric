# issue:4460873446 Callback Replay Validation

Plan reference: `plans/issue_4460873446_callback_replay_PLAN.md`

## Requirement status

- Complete: `CallbackService.replay_existing_for_persisted_key` is an explicit
  async method instead of a class-body alias.
- Complete: Both replay entry points still delegate to `_replay_existing_locked`,
  preserving the advisory-lock durable replay behavior.
- Complete: The callback service unit test now fails against the old alias
  implementation and verifies distinct public method names plus shared locked
  replay delegation.
- Complete: The callback route documents the accepted pre-admission durable DB
  probe trade-off for cold persisted idempotency replays.
- Complete: Narrow callback route/service validation passed.

## Evidence

Changed files:

- `src/awf/service/callbacks.py`
- `src/awf/api/routes/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/issue_4460873446_callback_replay_PLAN.md`
- `plans/issue_4460873446_callback_replay_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_callback_service_persisted_key_replay_is_explicit_locked_replay_path -q`
  - Failed before implementation because
    `CallbackService.replay_existing_for_persisted_key` was the same function
    object as `CallbackService.replay_existing`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py tests/unit/api/test_callbacks.py -q`
  - Passed: 136 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
