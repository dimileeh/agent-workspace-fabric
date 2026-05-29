# Review PRRT_kwDOSJAM6s6FhzOz Default Config Permissions Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6FhzOz_default_config_permissions_PLAN.md`

## Requirement Status

- Add a regression test showing an explicit default AWF config path gets a
  `0700` parent directory when created under a permissive umask: Complete.
  Added
  `test_host_setup_config_write_secures_explicit_default_parent_permissions`.
- Preserve the existing behavior that arbitrary explicit parent directories are
  not chmodded to `0700`: Complete. The existing explicit-parent regression
  still passes.
- Keep config file writes at `0600`: Complete. The new and existing permission
  tests assert `0600` config files.
- Run focused validation only for the touched host setup config tests: Complete.
  Full AWF/GitHub validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/host_setup/config.py`
- `tests/unit/service/test_host_setup_config.py`
- `plans/review_PRRT_kwDOSJAM6s6FhzOz_default_config_permissions_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6FhzOz_default_config_permissions_VALIDATION.md`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_secures_explicit_default_parent_permissions -q`
  failed because the explicit default `.awf` parent was `0755` instead of
  `0700`.

Passing focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_round_trips_with_conservative_permissions tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_preserves_explicit_parent_permissions tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_secures_explicit_default_parent_permissions -q`
  passed, 3 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed, 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  passed.
