# Validation: DX 10/10 Remediation

## Summary

The implementation matches the saved plan. First-run docs route to a canonical
quickstart, upgrade/release docs are discoverable, dense DX commands now have
curated terminal output, smoke reports discover the default local console URL,
and existing-profile onboarding no longer recommends rewriting the profile.

## Automated Checks

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py tests/unit/cli/test_profile_preview.py tests/unit/cli/test_smoke.py tests/unit/service/test_smoke.py tests/unit/docs/test_public_docs_status.py -q`
  - Passed: 150 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli tests/unit/service/test_readiness.py tests/unit/service/test_smoke.py tests/unit/docs/test_public_docs_status.py -q`
  - Passed: 293 tests after the review follow-up.
- Review follow-up: configured `AWF_CONSOLE_URL` smoke links now probe
  reachability before reporting `SMOKE_CONSOLE_READY`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py -q`
  - Passed: 52 tests after the review follow-up.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli src/awf/service tests/unit/cli tests/unit/service tests/unit/docs`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/cli src/awf/service`
  - Passed.

## Manual DX Checks

- `uv run --python 3.12 --extra dev awf init .`
  - Passed and reported the existing `.awf/workspace.yml` instead of suggesting
    profile creation.
- `uv run --python 3.12 --extra dev awf profile preview . --format pretty`
  - Passed with curated, non-flattened terminal output.
- `uv run --python 3.12 --extra dev awf smoke run --mocked-local --format pretty`
  - Passed with `status: ok` and `SMOKE_CONSOLE_READY` for
    `http://localhost:3000`.
- `uv run --python 3.12 --extra dev awf service status --format pretty`
  - Passed with local service status `ok`.
- `uv run --python 3.12 --extra dev awf service readiness --format pretty`
  - Correctly exited nonzero because 168-hour PRD SLO thresholds are below the
    Core release criteria; the output now labels this as release readiness, not
    local health.
- `uv run --python 3.12 --extra dev awf service release-readiness --format pretty`
  - Matched the same release gate behavior as `awf service readiness`.

## Gaps

- No OpenAPI check was run because this change does not modify REST schemas or
  API route behavior.
