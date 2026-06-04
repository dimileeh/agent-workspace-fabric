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

## CI Repair Iteration: Supported Script Surface

### Problem Statement And Scope

PR #394 CI reports that the docs/API surface cleanup test fails because
`scripts/first_run_smoke.py` now exists in `scripts/` but the supported script
allowlist still only names the older generator and release helper scripts. The
source smoke lanes reported in CI pass in this workspace with the focused repro,
so this repair is scoped to keeping the script-surface guard aligned with the
new first-run smoke harness.

### Requirements Checklist

- Preserve the script-surface guard; do not skip or loosen the test.
- Add `first_run_smoke.py` to the supported script surface because it is an
  intentional T14 smoke harness entrypoint.
- Re-run the focused CI repro command provided by AWF and record that broad
  AWF/GitHub validation remains deferred to AWF.

### Implementation Steps

1. Update `tests/unit/docs/test_api_surface_cleanup_docs.py` so
   `SUPPORTED_SCRIPTS` includes `first_run_smoke.py`.
2. Re-run the AWF-provided focused pytest command covering the script-surface
   guard and both first-run source smoke lanes.
3. Update `plans/T14_E2E_SMOKE_HARNESS_VALIDATION.md` with requirement status
   and focused evidence.

### Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  must pass after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation, provenance, logs, and merge gating after completion.
