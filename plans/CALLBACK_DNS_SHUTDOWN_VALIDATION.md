# Callback DNS Shutdown Validation

Plan reference: `CALLBACK_DNS_SHUTDOWN_PLAN.md`

## Requirement Status

- Add a regression test showing `shutdown_callback_target_validation_executor(wait=False)` allows a process to exit even when a DNS validation worker is still running: Complete.
- Keep callback DNS work isolated from asyncio's default executor: Complete.
- Preserve lazy executor creation and shutdown reset behavior: Complete.
- Avoid weakening existing callback safety policy or delivery tests: Complete.
- Run the narrow relevant test surface after implementation: Complete.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/CALLBACK_DNS_SHUTDOWN_PLAN.md`
- `plans/CALLBACK_DNS_SHUTDOWN_VALIDATION.md`

Verification:

- Confirmed the new regression failed before implementation: subprocess printed `shutdown-returned` but timed out because the standard executor worker kept the child process alive.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_callback_target_validation_executor_shutdown_does_not_keep_process_alive -q` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q` passed: 48 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/callbacks.py` passed.

No gaps remain against the saved plan.
