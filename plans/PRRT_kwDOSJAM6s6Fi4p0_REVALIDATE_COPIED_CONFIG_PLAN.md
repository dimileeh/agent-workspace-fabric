# Revalidate Copied Host Setup Config Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6Fi4p0` reports that callers can use
`HostSetupConfig.model_copy(update=...)` to place unvalidated provider payloads
on an otherwise frozen config model. `write_host_setup_config()` currently dumps
the model and scans for secret-looking payloads, but it does not re-run schema
validators before writing. A copied config can therefore persist invalid,
non-secret data that the next read rejects as corrupt.

Scope is limited to host setup config write-time validation and focused
regression coverage for copied models. Broad AWF/GitHub validation remains owned
by AWF after this agent cycle.

## Requirements Checklist

- Add a regression proving a `model_copy(update=...)` config with an invalid
  provider credential reference is rejected before any file is written.
- Re-run `HostSetupConfig` validation on the dumped write payload before YAML
  serialization reaches disk.
- Preserve existing secret-payload write errors and sanitized diagnostics.
- Keep the change localized to `src/awf/host_setup/config.py`, focused host
  setup config tests, and required plan/validation docs.
- Run only targeted tests or checks for the changed behavior.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_host_setup_config.py`
   for an unvalidated `model_copy(update=...)` provider payload.
2. Update `write_host_setup_config()` to validate the dumped payload as
   `HostSetupConfig` after the existing secret scan and before YAML output.
3. Preserve reason-coded `HostSetupConfigError` behavior for validation
   failures.
4. Run the focused new regression and the focused host setup config test file
   if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_revalidates_model_copy_updates -q`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Passes with the new regression and existing host setup config coverage.
- Full AWF/GitHub validation is intentionally not run in the agent phase per the
  workspace contract.
