# Production Configuration Guardrails Plan

## Problem Statement And Scope

AWF is local-first today, but `AWF_ENV=prod` must fail fast when bundled local
development defaults would otherwise become a network-facing production
configuration. This slice adds deterministic production-only validation without
changing local or CI runtime defaults.

Implementation is intentionally narrow: reusable settings guardrails, service
resolution and app startup call sites, focused tests, and concise operator docs.

## Requirements Checklist

- Keep local development defaults usable for `AWF_ENV=local` and `AWF_ENV=ci`.
- Treat `AWF_ENV=prod` as production. Do not add staging enforcement unless an
  existing project convention requires it.
- Reject the default local database URL or credentials in production:
  `postgresql+asyncpg://awf:awf_dev@localhost:5433/awf`.
- Reject production with missing `AWF_API_TOKEN`.
- Reject production with obviously weak or placeholder `AWF_API_TOKEN` values.
- Guard callback-enabled production posture using currently available settings.
  Since the base config exposes `callbacks_enabled` but no HTTPS or callback
  allowlist policy fields, require a strong API token when callbacks are enabled
  and document the deferred callback SSRF hardening work.
- Provide reusable helpers such as `settings_guardrails` and
  `validate_production_settings`.
- Call production validation during service settings resolution and app startup
  before admitting work or opening DB connections.
- Produce structured, actionable diagnostics without printing raw tokens,
  database passwords, or full secret-bearing URLs.
- Update docs for local-vs-production expectations and relevant environment
  variables.

## Implementation Steps

1. Add focused failing tests in `tests/unit/service/test_config.py` and
   `tests/unit/api/test_app_lifespan.py` for local/CI allowance, production DB
   rejection, token rejection, callback posture rejection, redaction, service
   resolution, and lifespan startup ordering.
2. Run the narrow test subset to confirm the missing behavior fails.
3. Add structured diagnostics and `ProductionSettingsError` in
   `src/awf/common/config.py`.
4. Add side-effect-free `settings_guardrails(...)` plus
   `validate_production_settings(...)`.
5. Validate after service-mode DB URL resolution in
   `src/awf/service/config.py`.
6. Validate in `src/awf/api/app.py` before `make_engine(...)`.
7. Update `docs/GETTING_STARTED.md` with production guardrail expectations.
8. Run the requested validation commands and fix failures with scoped changes.
9. Create `plans/PRODUCTION_CONFIG_GUARDRAILS_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

TDD failure check after tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/api/test_app_lifespan.py -q
```

Required final validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/api/test_app_lifespan.py tests/unit/test_postgres_only_edges.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: the new guardrail tests fail before implementation, pass after
implementation, and all requested final validation commands pass.
