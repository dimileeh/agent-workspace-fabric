# Nonempty Companion Env Secrets Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FXaSp_NONEMPTY_COMPANION_ENV_SECRETS_PLAN.md`

## Requirement Status

- Required companion env secrets with empty source values fail with
  `COMPANION_ENV_SECRET_SOURCE_MISSING`: Complete. The focused regression now
  asserts the resolver raises `ProfileResolutionError` with that reason code.
- Required companion env secrets render `${VAR:?err}`: Complete. Required
  placeholders now use Compose's unset-or-empty required interpolation form.
- Optional companion env secrets keep existing empty-value behavior: Complete.
  The focused `environment_secret` test selection passed, including the
  optional empty source case.
- Raw secret values are not placed into AWF service objects or rendered Compose
  YAML: Complete. Existing focused assertions for service and Compose rendering
  still pass.
- Validation stays focused: Complete. Only targeted unit tests and touched-file
  lint were run; full AWF/GitHub validation remains managed after agent
  completion.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`
- `tests/unit/node/test_stack_launcher.py`
- `tests/unit/node/test_compose_manager.py`
- `plans/PRRT_kwDOSJAM6s6FXaSp_NONEMPTY_COMPANION_ENV_SECRETS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FXaSp_NONEMPTY_COMPANION_ENV_SECRETS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_service_from_materialized_fails_required_empty_environment_secret_value -q`
  - Initial TDD run failed because the resolver accepted an empty required
    source value.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q -k "environment_secret"`
  - Passed: `10 passed, 34 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_resolves_companion_environment_secrets tests/unit/node/test_compose_manager.py::TestRender::test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py`
  - Passed.

## Remaining Gaps

None for the planned scope. Broad repository validation, coverage gates, and
CI-equivalent checks were intentionally not run in the agent phase per the AWF
workspace contract.
