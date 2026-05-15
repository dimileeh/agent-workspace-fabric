# PRRT_kwDOSJAM6s6CN7EU Validation

Plan reference: `PRRT_kwDOSJAM6s6CN7EU_PLAN.md`

## Requirement Status

- Reproduce the environment-variable path with a failing unit test: Complete.
  The new regression failed before implementation with a `SettingsError` from
  pydantic-settings JSON decoding `callbacks_allowed_hosts`.
- Preserve existing constructor parsing for comma-separated strings, lists, and
  tuples: Complete. The existing validator remains the parsing path.
- Allow `AWF_CALLBACKS_ALLOWED_HOSTS=operator.example.com,backup.example.com`
  to normalize to `("operator.example.com", "backup.example.com")`: Complete.
  `callbacks_allowed_hosts` now uses `NoDecode`, so raw env strings reach the
  validator.
- Keep invalid non-string/list/tuple values rejected with the existing message:
  Complete. Existing validation test coverage still passes.
- Avoid unrelated settings, callback delivery, or service behavior changes:
  Complete. Changes are limited to settings field annotation and focused tests.

## Evidence

Files changed:

- `src/awf/common/config.py`
- `tests/unit/common/test_common_polish.py`
- `plans/PRRT_kwDOSJAM6s6CN7EU_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CN7EU_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py::TestSettings::test_callback_allowed_hosts_accepts_comma_separated_env -q`
  failed before implementation with `SettingsError`, confirming the bug.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py -q`
  passed: 16 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/common/test_common_polish.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/common/config.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_enforces_callback_target_allowlist_policy -q`
  passed.
