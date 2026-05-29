# PRRT_kwDOSJAM6s6Fgl-E Config Parent Write Error Plan

## Problem Statement And Scope

`write_host_setup_config` creates the config parent directory before entering the
write `try` block. If that directory cannot be created, such as when a file
already exists at `~/.awf`, callers receive a raw `OSError` instead of the
reason-coded `HostSetupConfigError` used by the rest of the host setup config IO
path. Scope is limited to host setup config write error handling and focused
unit coverage.

## Requirements Checklist

- Add a regression test proving parent directory creation failures are reported
  as `HOST_SETUP_CONFIG_CORRUPT`.
- Preserve secret-payload validation behavior before filesystem writes.
- Preserve existing atomic write behavior, temp cleanup, and conservative
  permissions on successful writes.
- Avoid broad validation; AWF/GitHub own full validation after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_host_setup_config.py`
   where a file occupies the default `.awf` directory path and writing config
   must raise `HostSetupConfigError`.
2. Confirm the new test fails before the implementation change.
3. Move parent directory creation and best-effort directory chmod under the
   write `try` path so `OSError` subclasses are wrapped consistently.
4. Run focused validation for the changed test and touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_parent_creation_error_is_reason_coded -q`
  passes after failing before the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passes.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion.
