# Host Setup Recursive YAML Aliases Validation

Plan reference: `plans/host_setup_recursive_yaml_aliases_PLAN.md`

## Requirement Status

- Add a regression test showing a recursive YAML alias is reported as
  `HOST_SETUP_CONFIG_CORRUPT`: Complete.
- Preserve existing secret-key and secret-value rejection behavior and
  sanitized diagnostics: Complete.
- Prevent recursive container traversal in `_ensure_no_secret_payload`:
  Complete.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion: Complete.

## Evidence

- Changed `tests/unit/service/test_host_setup_config.py` with
  `test_host_setup_config_treats_recursive_yaml_alias_as_corrupt`.
- Changed `src/awf/host_setup/config.py` to detect recursive mappings and
  sequences during the secret-payload scan and convert that diagnostic to
  corrupt config during reads.
- Pre-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_treats_recursive_yaml_alias_as_corrupt -q`
  failed with `RecursionError`.
- Post-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py::test_host_setup_config_treats_recursive_yaml_alias_as_corrupt -q`
  passed.
- Focused test module:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  passed with 30 tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`
  passed.
- File-scoped type check:
  `uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py`
  passed.

Full AWF/GitHub validation is intentionally left to AWF after agent completion,
per the workspace contract.
