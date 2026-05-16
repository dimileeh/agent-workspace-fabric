# Command Watchdog CI Fix Validation

Plan reference: `plans/COMMAND_WATCHDOG_CI_FIX_PLAN.md`

## Requirement Status

- Complete: Keep the current AWF branch and do not push.
  - Evidence: Work stayed on the existing `awf/ws_a1b0d9e586c644d1ba4b5d60`
    branch.
- Complete: Preserve the subprocess cancellation assertion.
  - Evidence: `test_asyncio_runner_cancellation_terminates_subprocess` still
    cancels `runner.run_streaming(...)`, expects `asyncio.CancelledError`, and
    asserts that the child PID no longer exists.
- Complete: Remove the PID-file readiness race by waiting for parseable PID
  content.
  - Evidence: `tests/unit/common/test_command_watchdogs.py` now uses
    `_wait_for_pid_file()` to poll until the file contains a valid integer.
- Complete: Keep changes focused to the failing test and required
  plan/validation docs.
  - Evidence: Code changes are limited to the watchdog test helper and call
    site.
- Complete: Validate with the failing node ID and the watchdog test module.
  - Evidence: Commands below passed.
- Complete: Commit locally with a conventional commit message describing the CI
  root cause.
  - Evidence: This validation file and the fix are included in the local
    conventional commit created after validation.

## Command Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_command_watchdogs.py::test_asyncio_runner_cancellation_terminates_subprocess -q`
  - Passed: `1 passed in 0.44s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/common/test_command_watchdogs.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope tests/unit/common/test_command_watchdogs.py -q`
  - Passed: `11 passed in 9.05s`
- `timeout 120s bash -lc 'for i in $(seq 1 20); do uv run --python 3.12 --extra dev pytest tests/unit/common/test_command_watchdogs.py::test_asyncio_runner_cancellation_terminates_subprocess -q >/tmp/awf-watchdog-test-fixed.out 2>&1 || { cat /tmp/awf-watchdog-test-fixed.out; exit 1; }; done; cat /tmp/awf-watchdog-test-fixed.out'`
  - Passed: final iteration reported `1 passed in 0.46s`

## Gaps

None. Full coverage was not rerun locally because the failure was reproduced
with the focused node ID and the fix was validated against the affected module
under the CI xdist shape.
