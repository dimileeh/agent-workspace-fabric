# T14 E2E Smoke Harness Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6G__Cc` reports that
`scripts/first_run_smoke.py` passes a timeout to `subprocess.run` but lets
`subprocess.TimeoutExpired` escape. The fix is scoped to reporting smoke command
timeouts as ordinary failed command results so the harness can continue
formatting diagnostics instead of crashing.

## Requirements Checklist

- Add a focused regression test for `run_command` timeout handling.
- Return a `subprocess.CompletedProcess[str]` with non-zero timeout status when
  `subprocess.run` raises `TimeoutExpired`.
- Preserve captured stdout/stderr, including byte output from timeout
  exceptions, and include a clear timeout message in stderr.
- Keep the change limited to the first-run smoke harness and its focused tests.

## Implementation Steps

1. Add a unit test in `tests/unit/scripts/test_first_run_smoke.py` that
   monkeypatches `subprocess.run` to raise `TimeoutExpired`.
2. Run that single test and confirm it fails before the code change.
3. Catch `TimeoutExpired` in `scripts/first_run_smoke.py::run_command`, convert
   any captured output to text, and return code `124`.
4. Re-run the focused first-run smoke unit tests touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  must pass after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
