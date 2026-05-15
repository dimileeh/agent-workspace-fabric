# Comment 4454303511 Setup Retry Metadata Validation

Plan reference: `comment_4454303511_SETUP_RETRY_METADATA_PLAN.md`

## Requirement Status

- Complete: Preserve command-level `ValidationCommandResult.retry_count` as the
  total number of retries for the command. Evidence:
  `test_setup_dependency_retry_does_not_consume_flaky_retry_budget` still asserts
  `command.retry_count == 2` for one setup-network retry plus one flaky retry.
- Complete: Report setup dependency metadata `retry_count` as setup dependency
  network retries so it matches the setup-only `attempts` list. Evidence: the
  mixed retry regression now asserts metadata `retry_count == 1` and
  `len(attempts) == 1`.
- Complete: Add explicit retry counters for observer clarity. Evidence:
  `_with_setup_dependency_network_metadata` now writes `setup_retry_count`,
  `flaky_retry_count`, and `total_retry_count`.
- Complete: Preserve the executor-side observability isolation fix. Evidence:
  `src/awf/control/executor.py` already wraps
  `_record_setup_dependency_network_events` in a local `try/except` and logs
  `executor.setup_dependency_network_event_record_failed` before continuing.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget -q`
  first failed against the old metadata contract with `assert 2 == 1`, then
  passed after the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed: 197 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
