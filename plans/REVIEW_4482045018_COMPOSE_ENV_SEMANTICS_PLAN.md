# Review 4482045018 Compose Env Semantics Plan

## Problem Statement And Scope

Address the latest review-level feedback for PR comment `issue:4482045018`.
Scope is limited to local-service Compose environment resolution:

- `awf service logs` should forward Compose interpolation variables based on the
  active Compose file, not a hand-maintained single-key allowlist.
- Status, bootstrap, doctor, readiness, and init should keep reading a root
  `.env` fallback when `docker/compose/.env` is absent, but should not forward
  that fallback through downstream `compose_env_file` / bootstrap `env_file`
  parameters that represent the actual Compose env file.

## Requirements Checklist

- [x] Add a failing service logs regression for a Compose interpolation variable
  other than `AWF_POSTGRES_PASSWORD`.
- [x] Derive service logs interpolation variables from the Compose YAML while
  preserving the secret-suppression contract for unrelated service env values.
- [x] Separate the active service env read source from the Compose env file path
  passed to downstream status/bootstrap/doctor/readiness/init helpers.
- [x] Update source-checkout root `.env` fallback tests so the root values still
  feed settings and provider env, while `compose_env_file` / bootstrap
  `env_file` is not the root fallback.
- [x] Run focused service logs and service CLI tests plus narrow lint.
- [x] Create a validation document mapping evidence to this plan.
- [x] Commit only the scoped files on the current AWF branch.

## Implementation Steps

1. Add tests for dynamic Compose interpolation extraction and for root `.env`
   fallback not being passed as a Compose env file in source-checkout service
   commands.
2. Confirm the focused tests fail against the current implementation.
3. Update `src/awf/service/logs.py` to parse interpolation variable names from
   the resolved Compose file and forward only those resolved values plus Docker
   host selection.
4. Add a CLI helper that returns both the env read source and the actual Compose
   env file, then use it in init and service commands.
5. Run focused pytest and ruff checks.
6. Record validation evidence in
   `plans/REVIEW_4482045018_COMPOSE_ENV_SEMANTICS_VALIDATION.md`.
7. Stage the changed files and commit with a conventional review-fix message.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_passes_existing_root_env_file_when_compose_env_is_missing tests/unit/cli/test_service_cli.py::test_service_bootstrap_cli_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_status_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_doctor_bundle_resolves_existing_root_env -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/logs.py tests/unit/service/test_logs.py tests/unit/cli/test_service_cli.py
```

Pass criteria: all listed commands pass after implementation, with any unrelated
environmental blocker documented in the validation file.
