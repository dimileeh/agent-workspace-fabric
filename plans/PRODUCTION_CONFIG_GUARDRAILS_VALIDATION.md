# Production Configuration Guardrails Validation

Plan reference: `plans/PRODUCTION_CONFIG_GUARDRAILS_PLAN.md`

## Requirement Status

- Complete: `AWF_ENV=local` and `AWF_ENV=ci` keep local development defaults
  usable. Evidence: `test_production_guardrails_allow_local_defaults` and
  `test_production_guardrails_allow_ci_defaults`.
- Complete: `AWF_ENV=prod` is the production mode for this slice; no staging
  enforcement was added.
- Complete: Production rejects the bundled local database URL and default
  `awf` / `awf_dev` credentials. Evidence:
  `test_production_guardrails_reject_default_local_database_url`.
- Complete: Production rejects missing, blank, short, and placeholder
  `AWF_API_TOKEN` values. Evidence:
  `test_production_guardrails_reject_missing_or_weak_api_token`.
- Complete: Callback-enabled production without a strong API token is rejected
  using currently available settings. Evidence:
  `test_production_guardrails_reject_callback_posture_without_api_token`.
- Complete: Reusable guardrails are implemented in `src/awf/common/config.py`
  as `settings_guardrails` and `validate_production_settings`.
- Complete: Service settings resolution validates after local service database
  URL defaulting. Evidence:
  `test_service_settings_resolution_runs_production_guardrails_after_db_resolution`.
- Complete: FastAPI lifespan validates before engine construction. Evidence:
  `test_lifespan_validates_production_settings_before_engine_creation`.
- Complete: Diagnostics are structured and redact sensitive values. Evidence:
  `ProductionSettingsDiagnostic`, `ProductionSettingsError`, and
  `test_production_guardrail_diagnostics_redact_sensitive_values`.
- Complete: Documentation explains local-vs-production expectations and the
  callback hardening deferral in `docs/GETTING_STARTED.md`.

## Validation Evidence

Initial TDD failure before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/api/test_app_lifespan.py -q
# failed during collection because ProductionSettingsError/settings_guardrails/
# validate_production_settings did not exist yet
```

Final requested validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/api/test_app_lifespan.py tests/unit/test_postgres_only_edges.py -q
# 71 passed in 1.59s

uv run --python 3.12 --extra dev ruff check src/awf tests
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 155 source files
```

## Gaps

None.
