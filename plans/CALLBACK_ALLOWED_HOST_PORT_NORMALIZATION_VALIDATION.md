# Callback Allowed Host Port Normalization Validation

Plan reference: `CALLBACK_ALLOWED_HOST_PORT_NORMALIZATION_PLAN.md`

## Requirement Status

- Add a regression test proving allowlist entries with port suffixes normalize
  to bare hostnames: Complete.
- Preserve existing normalization for comma-separated environment values,
  whitespace, trailing dots, and lowercase hostnames: Complete.
- Implement the smallest code change needed in `src/awf/common/config.py`:
  Complete.
- Run the narrow unit test that covers the change: Complete.
- Commit the thread-specific fix locally without pushing: Complete.

## Evidence

Files changed:

- `src/awf/common/config.py`
- `tests/unit/common/test_common_polish.py`
- `plans/CALLBACK_ALLOWED_HOST_PORT_NORMALIZATION_PLAN.md`
- `plans/CALLBACK_ALLOWED_HOST_PORT_NORMALIZATION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py::TestSettings::test_callback_allowed_hosts_strips_port_suffixes -q`
  failed before implementation with the expected port-normalization assertion.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py::TestSettings::test_callback_allowed_hosts_strips_port_suffixes -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py -q`
  passed with 21 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/common/test_common_polish.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
