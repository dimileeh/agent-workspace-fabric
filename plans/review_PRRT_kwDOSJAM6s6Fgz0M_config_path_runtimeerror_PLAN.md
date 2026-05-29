# Review PRRT_kwDOSJAM6s6Fgz0M Config Path RuntimeError Plan

## Problem Statement And Scope

PR review feedback reports that host setup config path resolution can leak raw
`RuntimeError` exceptions from `Path.home()` or `Path.expanduser()` when a home
directory cannot be determined. That bypasses the reason-coded
`HostSetupConfigError` contract used by `read_host_setup_config` and
`write_host_setup_config`.

Scope is limited to host setup config path resolution in
`src/awf/host_setup/config.py`, focused regression coverage in
`tests/unit/service/test_host_setup_config.py`, and this plan/validation record.

## Requirements Checklist

- Preserve normal default and explicit host setup config path resolution.
- Convert `Path.home()` failures during default config path resolution into
  reason-coded `HostSetupConfigError`.
- Convert `Path.expanduser()` failures during explicit config path resolution
  and explicit default-path home argument handling into reason-coded
  `HostSetupConfigError`.
- Cover both read and write entry points for the failing path resolution cases.
- Cover the public `default_host_setup_config_path(home=...)` expansion case.
- Do not run AWF/GitHub-owned broad validation; use narrow local checks only.

## Implementation Steps

1. Add focused failing tests for default-path home resolution failure and
   explicit-path expanduser failure across read and write helpers, plus the
   explicit `home` branch of `default_host_setup_config_path`.
2. Update config path resolution helpers to catch `OSError` and `RuntimeError`
   from `Path.home()` and `Path.expanduser()`, then raise sanitized
   `HostSetupConfigError` diagnostics.
3. Run the focused regression tests before and after implementation when
   practical.
4. Run a narrow lint check for the touched code and test files.
5. Record validation evidence in
   `plans/review_PRRT_kwDOSJAM6s6Fgz0M_config_path_runtimeerror_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "config_path_resolution_failure"`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "host_setup_config or config_path_resolution_failure"`
  must pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  must pass.
- Full AWF/GitHub validation is intentionally not run during the agent phase.
