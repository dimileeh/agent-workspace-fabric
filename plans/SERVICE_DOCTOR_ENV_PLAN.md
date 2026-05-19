# Service Doctor Env Plan

## Problem Statement and Scope

PR review comment `issue:4482045018` identified two follow-up gaps in the local service env-resolution work:

- `awf service doctor` still resolves settings and local service environment through CWD-relative defaults, while `awf service status` and `awf service bootstrap` now use `_resolve_service_env_paths()`.
- `_init_env_warning()` calls `_init_display_path()` on fields already display-normalized by `_init_env_error_payload()`.

Scope is limited to CLI behavior and focused unit coverage.

## Requirements Checklist

- Add a regression proving `awf service doctor` resolves settings and provider environment from the resolved compose env file.
- Update `service_doctor` to use `_resolve_service_env_paths()`, `local_service_environ(env_file=...)`, and `resolve_service_settings(Settings(_env_file=...), environ=...)`.
- Preserve `service_doctor --bundle` behavior by passing the same resolved settings and environment to support bundle collection.
- Add or update coverage proving `_init_env_warning()` uses already display-ready payload values directly.
- Run focused unit tests for the changed CLI behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/cli/test_service_cli.py` and/or `tests/unit/cli/test_init.py`.
2. Update `src/awf/cli/main.py` for service doctor env resolution and warning formatting.
3. Run the focused tests and fix any regressions.
4. Create `plans/SERVICE_DOCTOR_ENV_VALIDATION.md` with requirement-by-requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_compose_env tests/unit/cli/test_init.py::test_init_env_warning_uses_display_ready_payload_paths -q`
- Pass criteria: both tests pass and no unrelated files are modified.
