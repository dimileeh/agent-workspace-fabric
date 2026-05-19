# PRRT_kwDOSJAM6s6DDvTD Plan

## Problem Statement And Scope

The unresolved review thread reports that `awf service doctor` resolves a
local-service env file in the CLI, but only passes the resolved compose file to
doctor diagnostics. `collect_doctor_report()` then rediscovers the compose env
file from the current working directory and can pair the AWF compose file with
an unrelated `docker/compose/.env`, especially when the active AWF checkout uses
an existing repo-root `.env`.

Scope is limited to forwarding the resolved env file into doctor diagnostics
and support bundles, plus regression tests for the reported mismatch.

## Requirements Checklist

- Add a regression proving `awf service doctor` passes the resolved root `.env`
  to doctor diagnostics when `docker/compose/.env` is absent.
- Add or update coverage proving support bundle collection forwards an explicit
  compose env file to the nested doctor collector.
- Let `collect_doctor_report()` accept an explicit compose env file and use it
  for both service environment loading and the worker `docker compose --env-file`
  check.
- Preserve existing autodiscovery behavior for callers that do not pass an
  explicit compose env file.
- Run the focused tests and lint for touched Python files.
- Commit the fix locally with a conventional commit referencing the review
  thread.

## Implementation Steps

1. Add failing regression assertions in CLI, support-bundle, and doctor unit
   tests.
2. Run the focused regression tests before implementation and confirm failure.
3. Add `compose_env_file` plumbing to `collect_doctor_report()` and
   `collect_support_bundle()`.
4. Pass the resolved env file from service doctor and bootstrap doctor
   preflight calls.
5. Run targeted tests and ruff.
6. Write `plans/PRRT_kwDOSJAM6s6DDvTD_VALIDATION.md` with requirement status
   and evidence, then commit the changed files.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_existing_root_env tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_compose_paths_to_doctor_collector tests/unit/service/test_doctor.py::test_doctor_worker_inspection_uses_explicit_compose_env_file -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_compose_env tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_doctor_bundle_resolves_existing_root_env tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_compose_paths_to_doctor_collector tests/unit/service/test_doctor.py::test_doctor_worker_inspection_loads_local_compose_env_file tests/unit/service/test_doctor.py::test_doctor_worker_inspection_uses_explicit_compose_env_file -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/doctor/__init__.py src/awf/service/support_bundle.py tests/unit/cli/test_service_cli.py tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py
```

Pass criteria: the pre-fix focused regression fails, then the targeted tests and
lint exit zero after implementation.
