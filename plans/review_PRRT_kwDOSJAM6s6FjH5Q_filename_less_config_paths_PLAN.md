# Review PRRT_kwDOSJAM6s6FjH5Q Filename-Less Config Paths Plan

## Problem Statement And Scope

The PR review thread reports that `write_host_setup_config()` constructs an
atomic temporary path with `Path.with_name()` before entering its write-failure
wrapper. Filename-less paths such as `Path(".")`, `Path("")`, or `Path("/")`
raise `ValueError` there and bypass the `HOST_SETUP_CONFIG_WRITE_FAILED`
contract.

Scope is limited to host setup config write-path error handling, a focused
regression test, and this plan/validation record.

## Requirements Checklist

- Add a regression test proving filename-less config paths are reason-coded as
  `HOST_SETUP_CONFIG_WRITE_FAILED`.
- Preserve sanitized diagnostics without leaking raw exception messages.
- Keep normal atomic writes and existing write-failure behavior unchanged.
- Run only focused checks for the touched host setup config behavior; broad
  AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a parametrized unit regression for `Path(".")`, `Path("")`, and
   `Path("/")`.
2. Confirm the new regression fails against the current implementation.
3. Update `write_host_setup_config()` so temporary-path construction failures
   are converted to `HostSetupConfigError` with
   `HOST_SETUP_CONFIG_WRITE_FAILED`.
4. Re-run the focused regression and relevant host setup config unit tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passes.
- Full AWF/GitHub validation is intentionally not run during the agent phase.

## Assumptions/Changes

- The regression uses `Path()` as the Ruff-compliant equivalent of `Path(".")`
  and `""` to exercise the empty `--config` input before path normalization.
