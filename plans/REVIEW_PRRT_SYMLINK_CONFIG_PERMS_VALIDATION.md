# Review PRRT Symlink Config Permissions Validation

Plan reference: `REVIEW_PRRT_SYMLINK_CONFIG_PERMS_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving an explicit symlink to the default
  config path does not chmod the caller-owned parent directory.
- Complete: Preserved the behavior that writing the actual default config path
  secures the default parent directory through the existing write-permission
  test coverage.
- Complete: Updated the config path identity helper to normalize path names
  without following symlinks.
- Complete: Ran focused validation only. Full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

- Changed `src/awf/host_setup/config.py` so `_normalized_config_path()` uses
  lexical absolute normalization instead of `Path.resolve(strict=False)`.
- Changed `tests/unit/service/test_host_setup_config.py` with
  `test_host_setup_config_write_preserves_parent_for_explicit_symlink_to_default`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -k "explicit_symlink_to_default" -q`
  failed because the shared parent mode changed from `0755` to `0700`.
- Confirmed the new regression passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -k "explicit_symlink_to_default" -q`
  passed with `1 passed, 58 deselected`.
- Focused test validation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -k "host_setup_config_write or config_path_helpers" -q`
  passed with `9 passed, 50 deselected`.
- Focused lint validation:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
