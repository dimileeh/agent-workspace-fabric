# Command Watchdog CI Fix Plan

## Problem Statement And Scope

PR #258 fails the `python-full-coverage` GitHub Actions job because
`tests/unit/common/test_command_watchdogs.py::test_asyncio_runner_cancellation_terminates_subprocess`
can observe an empty `child.pid` file. The test waits only for path existence,
but `Path.write_text()` creates/truncates the file before PID bytes are
available, so CI under xdist/coverage can read `""` and fail before exercising
subprocess cancellation.

Scope is limited to making the watchdog cancellation regression test wait for
actual child readiness without skipping, weakening, or disabling the check.

## Requirements Checklist

- Keep the current AWF branch and do not push.
- Preserve the subprocess cancellation assertion.
- Remove the PID-file readiness race by waiting for parseable PID content.
- Keep changes focused to the failing test and required plan/validation docs.
- Validate with the failing node ID and the watchdog test module.
- Commit locally with a conventional commit message describing the CI root
  cause.

## Implementation Steps

1. Replace the file-existence helper in `tests/unit/common/test_command_watchdogs.py`
   with a helper that polls until the PID file contains a valid integer.
2. Update `test_asyncio_runner_cancellation_terminates_subprocess` to use the
   helper and keep the existing cancellation and cleanup assertions.
3. Run the focused failing node ID.
4. Run the watchdog test module.
5. Create `plans/COMMAND_WATCHDOG_CI_FIX_VALIDATION.md` with requirement
   status and command evidence.
6. Commit the fix locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_command_watchdogs.py::test_asyncio_runner_cancellation_terminates_subprocess -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_command_watchdogs.py -q`
  passes.
