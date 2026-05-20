# Public Compose Env Sentinel Validation

Plan reference:
`plans/REVIEW_4482045018_PUBLIC_COMPOSE_ENV_SENTINEL_PLAN.md`

## Requirement Status

- Expose public sentinel symbols from `awf.service.config`: Complete.
  `ComposeEnvFileOmitted`, `COMPOSE_ENV_FILE_OMITTED`, and
  `ComposeEnvFileInput` are public names and included in `__all__`.
- Update cross-module imports and type checks to use public names: Complete.
  `status.py`, `readiness.py`, `doctor/__init__.py`, and
  `support_bundle.py` now import and use the public symbols.
- Preserve existing omitted-vs-explicit-null behavior: Complete. The singleton
  and `isinstance` checks are unchanged except for the public names.
- Add or update focused regression coverage for the public contract: Complete.
  `tests/unit/service/test_config.py` verifies the exported public names and
  rejects private sentinel imports from the service modules.
- Run the narrowest relevant validation commands: Complete.

## Evidence

Files changed:

- `src/awf/service/config.py`
- `src/awf/service/status.py`
- `src/awf/service/readiness.py`
- `src/awf/service/doctor/__init__.py`
- `src/awf/service/support_bundle.py`
- `tests/unit/service/test_config.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_compose_env_file_sentinel_is_public_service_contract -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py src/awf/service/status.py src/awf/service/readiness.py src/awf/service/doctor/__init__.py src/awf/service/support_bundle.py tests/unit/service/test_config.py`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/service/test_status.py tests/unit/service/test_readiness.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py -q`
- `git diff --check`

Results:

- Focused regression test: `1 passed`.
- Full lint: passed.
- Mypy: passed, `Success: no issues found in 158 source files`.
- Affected service test batch: `245 passed`.
- Diff whitespace check: passed.

No remaining gaps.
