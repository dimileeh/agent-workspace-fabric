# Review 4482312648 Dotenv Cache and Compose Escape Plan

## Problem Statement and Scope

Address the two actionable observations from PR review comment `issue:4482312648`:

- Avoid repeated project `.env` candidate discovery when a single service-settings resolution checks both database and API default values.
- Bring the local Compose template evaluator used by integration tests closer to Docker Compose semantics for `$$` literal-dollar escapes.

Scope is limited to service configuration resolution helpers, their focused unit tests, the integration Compose evaluator helper, and mandatory plan/validation artifacts.

## Requirements Checklist

- Add a regression test proving project dotenv candidate discovery is shared within one `resolve_service_settings` call when both explicitness checks need it.
- Implement the smallest config change that caches/reuses project dotenv lookup state within a `resolve_service_settings` call without changing explicit override semantics.
- Add a regression test proving `$${VAR:-default}` remains a literal `${VAR:-default}` in the Compose evaluator.
- Update the Compose evaluator helper to handle `$$` as a literal `$` while preserving existing interpolation forms.
- Run the narrow tests for the touched config and Compose helper areas.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_config.py` and `tests/integration/test_local_service_compose.py`.
2. Introduce a per-resolution project dotenv lookup cache and thread it through the service config explicitness checks.
3. Replace the regex-only Compose evaluator substitution with a small scanner that honors `$$` before expanding `${...}`.
4. Run targeted pytest commands, then lint/typecheck if the touched Python surface warrants it.
5. Record validation evidence in `plans/REVIEW_4482312648_DOTENV_CACHE_COMPOSE_ESCAPE_VALIDATION.md`.
6. Stage only changed files and commit with a conventional review-comment message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
- `uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py tests/integration/test_local_service_compose.py`

All commands must pass. Any remaining gap must be documented in the validation file before completion.
