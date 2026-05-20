# Review 4482312648 Dotenv Cache and Compose Escape Validation

Plan reference: `plans/REVIEW_4482312648_DOTENV_CACHE_COMPOSE_ESCAPE_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving project dotenv candidate discovery is shared within one `resolve_service_settings` call when both database and API explicitness checks need it.
- Complete: Implemented a per-resolution `_ProjectDotenvLookup` cache and threaded it through the service config explicitness checks without changing override semantics.
- Complete: Added a regression test proving `$${VAR:-default}` remains a literal `${VAR:-default}` in the Compose evaluator.
- Complete: Updated the Compose evaluator helper to handle `$$` as a literal `$` before expanding `${...}`.
- Complete: Ran the planned narrow tests and lint for the touched files.
- Complete: Ran `mypy src/awf` because the config change introduced typed helper plumbing.

## Evidence

Files changed:

- `src/awf/service/config.py`
- `tests/unit/service/test_config.py`
- `tests/integration/test_local_service_compose.py`
- `plans/REVIEW_4482312648_DOTENV_CACHE_COMPOSE_ESCAPE_PLAN.md`
- `plans/REVIEW_4482312648_DOTENV_CACHE_COMPOSE_ESCAPE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_resolve_service_settings_reuses_project_dotenv_candidates_for_default_url_checks -q` failed before implementation with `assert 2 == 1`, then passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py::test_compose_template_value_matches_common_interpolation_forms -q` failed before implementation with `$15433 != ${AWF_ESCAPED:-5433}`, then passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q` passed: `102 passed`.
- `uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py -q` passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py tests/integration/test_local_service_compose.py` passed.
- `uv run --python 3.12 --extra dev ruff format src/awf/service/config.py` applied formatting required by the commit hook.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/config.py tests/unit/service/test_config.py tests/integration/test_local_service_compose.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
