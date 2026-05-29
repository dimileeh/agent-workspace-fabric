# Host Setup Config Parent Permissions Validation

Plan reference: `plans/HOST_SETUP_CONFIG_PARENT_PERMISSIONS_PLAN.md`

## Requirement Status

- Preserve conservative `0700` parent permissions for the helper-owned default
  AWF config directory: Complete.
- Do not chmod the parent directory for an explicit caller-supplied config path:
  Complete.
- Continue writing config files atomically with `0600` file permissions:
  Complete.
- Add a regression test for the explicit-path parent permission behavior:
  Complete.
- Run focused tests for `tests/unit/service/test_host_setup_config.py` only:
  Complete.

## Evidence

- Changed `src/awf/host_setup/config.py` so parent chmod is applied only when
  `write_host_setup_config()` is using the default path (`path is None`).
- Updated `tests/unit/service/test_host_setup_config.py` so the default-path
  permissions test exercises the helper-owned path, and added a regression for
  explicit caller-owned parent directories.
- Confirmed the new regression failed before the implementation change:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_write_preserves_explicit_parent_permissions -q`.
- Focused verification passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  (`31 passed`).
- Focused lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`.

Full AWF/GitHub validation and merge-gate provenance were not run in the agent
phase; AWF owns that broader validation after completion.
