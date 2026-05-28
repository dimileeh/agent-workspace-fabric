# Review PRRT_kwDOSJAM6s6Fgz0M Config Path RuntimeError Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6Fgz0M_config_path_runtimeerror_PLAN.md`

## Requirement Status

- Complete: Preserve normal default and explicit host setup config path
  resolution.
- Complete: Convert `Path.home()` failures during default config path
  resolution into reason-coded `HostSetupConfigError`.
- Complete: Convert `Path.expanduser()` failures during explicit config path
  resolution and explicit default-path home argument handling into reason-coded
  `HostSetupConfigError`.
- Complete: Cover both read and write entry points for the failing path
  resolution cases.
- Complete: Cover the public `default_host_setup_config_path(home=...)`
  expansion case.
- Complete: Do not run AWF/GitHub-owned broad validation; use narrow local
  checks only.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/review_PRRT_kwDOSJAM6s6Fgz0M_config_path_runtimeerror_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6Fgz0M_config_path_runtimeerror_VALIDATION.md`

Focused checks:

- Pre-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "config_path_resolution_failure"`
  failed with four raw `RuntimeError` leaks from `Path.home()` and
  `Path.expanduser()`.
- Post-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "config_path_resolution_failure"`
  passed with 5 tests.
- Targeted behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "host_setup_config or config_path_resolution_failure"`
  passed with 27 tests.
- Narrow lint check:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- Narrow type check:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  passed.

Full AWF/GitHub validation was intentionally not run during the agent phase; AWF
owns the broad validation and merge-gating surface after agent completion.
