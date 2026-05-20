# Compose Interpolation Defaults Validation

Plan reference: `COMPOSE_INTERPOLATION_DEFAULTS_PLAN.md`

## Requirement Status

- Add a regression proving `$BAR` inside `${FOO:-$BAR}` is not collected as a Compose interpolation input: Complete.
- Preserve existing behavior for braced variables, plain variables, escaped dollar values, malformed braced expressions, and YAML-value-only collection: Complete.
- Do not change cache behavior; the review note about empty tuple caching is already correct and needs no code change: Complete.
- Run the narrow unit test surface that covers service log interpolation parsing: Complete.

## Evidence

Files changed:

- `src/awf/service/environment.py`
- `tests/unit/service/test_logs.py`
- `plans/COMPOSE_INTERPOLATION_DEFAULTS_PLAN.md`
- `plans/COMPOSE_INTERPOLATION_DEFAULTS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q -k 'braced_defaults or unclosed_braced_compose_interpolation or mapping_key_interpolation'` failed before implementation with `AWF_LITERAL_FALLBACK` over-collected.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q -k 'braced_defaults or unclosed_braced_compose_interpolation or mapping_key_interpolation or passes_compose_interpolation_values'` passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py tests/unit/service/test_logs.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/environment.py` passed.

## Gaps

None.
