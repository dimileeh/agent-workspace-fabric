# Companion Env Secret Required Interpolation Validation

Plan reference:
`plans/COMPANION_ENV_SECRET_REQUIRED_INTERPOLATION_PLAN.md`

## Requirement Status

- Required companion env secrets render a Compose required interpolation form:
  Complete. `_resolve_environment_secrets()` now emits `${VAR?err}` for required
  env-backed companion secrets.
- Raw secret values are not placed into AWF service objects or compose YAML:
  Complete. Existing focused assertions still verify the raw source value is not
  present in rendered service/compose representations.
- Explicitly empty source values remain accepted:
  Complete. The focused empty-value regression remains and expects unset-only
  required interpolation.
- Optional missing companion env secrets continue to be omitted:
  Complete. Existing focused optional omission tests remained green, and an
  optional-present assertion verifies optional refs keep non-required
  interpolation.
- Validation remains focused:
  Complete. Only touched node behavior tests and touched-file lint were run.
  Full AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`
- `tests/unit/node/test_stack_launcher.py`
- `tests/unit/node/test_compose_manager.py`
- `plans/COMPANION_ENV_SECRET_REQUIRED_INTERPOLATION_PLAN.md`
- `plans/COMPANION_ENV_SECRET_REQUIRED_INTERPOLATION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py -q`
  - Initial TDD run failed on the old plain `${ANTHROPIC_API_KEY}` rendering.
  - Final run passed: `99 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py`
  - Passed.

## Remaining Gaps

None for the planned scope. Broader validation, coverage gates, and CI-equivalent
checks were intentionally not run in the agent phase per the AWF workspace
contract.
