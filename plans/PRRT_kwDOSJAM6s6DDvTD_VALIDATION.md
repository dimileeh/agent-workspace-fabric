# PRRT_kwDOSJAM6s6DDvTD Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DDvTD_PLAN.md`

## Requirement Status

- Complete: Added CLI regression coverage proving `awf service doctor` forwards
  the resolved compose `.env` and root `.env` paths to doctor diagnostics.
- Complete: Added CLI bundle coverage proving `awf service doctor --bundle`
  forwards the resolved root `.env` path to support-bundle collection.
- Complete: Updated support-bundle coverage proving an explicit
  `compose_env_file` is forwarded to the nested doctor collector.
- Complete: `collect_doctor_report()` now accepts an explicit
  `compose_env_file`, uses it for service environment loading, and uses that
  path for the worker `docker compose --env-file` check when it exists.
- Complete: Existing autodiscovery behavior is preserved for callers that omit
  `compose_env_file`.

## Evidence

Changed files:

- `src/awf/cli/main.py`
- `src/awf/service/doctor/__init__.py`
- `src/awf/service/support_bundle.py`
- `tests/unit/cli/test_service_cli.py`
- `tests/unit/service/test_doctor.py`
- `tests/unit/service/test_support_bundle.py`

Pre-fix regression evidence:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_existing_root_env tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_compose_paths_to_doctor_collector tests/unit/service/test_doctor.py::test_doctor_worker_inspection_uses_explicit_compose_env_file -q
```

Result before implementation: failed as expected because `compose_env_file` was
missing or unsupported.

Post-fix verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_compose_env tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_doctor_bundle_resolves_existing_root_env tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_compose_paths_to_doctor_collector tests/unit/service/test_doctor.py::test_doctor_worker_inspection_loads_local_compose_env_file tests/unit/service/test_doctor.py::test_doctor_worker_inspection_uses_explicit_compose_env_file -q
```

Result: 6 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/doctor/__init__.py src/awf/service/support_bundle.py tests/unit/cli/test_service_cli.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py
```

Result: all checks passed.

```bash
uv run --python 3.12 --extra dev ruff format --check tests/unit/cli/test_service_cli.py
```

Result: file already formatted.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: success, no issues found.

```bash
git diff --check
```

Result: no whitespace errors.

## Remaining Gaps

None.
