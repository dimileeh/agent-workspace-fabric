# PRRT_kwDOSJAM6s6FZC_h Required Secret Resume Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FZC_h_REQUIRED_SECRET_RESUME_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving missing required companion env secret
  sources on monitor resume record `COMPANION_ENV_SECRET_SOURCE_MISSING`.
- Complete: Added coverage proving empty required companion env secret sources
  record `COMPANION_ENV_SECRET_SOURCE_EMPTY`.
- Complete: Added a required env-backed companion secret preflight before
  optional-secret refresh and `ensure_project_up`.
- Complete: Preserved optional companion env-secret refresh behavior and avoided
  logging raw secret values.
- Complete: Preserved non-terminal monitor-resume behavior by routing preflight
  failures through the existing monitor runtime restart failure event and then
  continuing to the PR monitor.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `plans/PRRT_kwDOSJAM6s6FZC_h_REQUIRED_SECRET_RESUME_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FZC_h_REQUIRED_SECRET_RESUME_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "preserves_required_companion_env_secret_reason_code"`
  - Result before implementation: failed for both missing and empty cases because
    resume called Compose and recorded `COMPOSE_COMMAND_FAILED`.
  - Result after implementation: passed, `2 passed, 14 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "required_companion_env_secret or optional_companion_env_secret or compose_failure_records_warning"`
  - Result: passed, `6 passed, 10 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py`
  - Result: passed.

Full AWF/GitHub validation was not executed during the agent phase per the
workspace contract; AWF owns broad validation and merge gating after completion.

## Remaining Gaps

None.
