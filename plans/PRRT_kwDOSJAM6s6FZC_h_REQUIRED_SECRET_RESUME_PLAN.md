# PRRT_kwDOSJAM6s6FZC_h Required Secret Resume Plan

## Problem Statement and Scope

PR #292 review thread `PRRT_kwDOSJAM6s6FZC_h` reports that monitor resume restarts
only repair optional companion env secrets before calling Docker Compose. If a
required env-backed companion secret source is missing or empty on the resumed
worker, Compose interpolation fails later and the monitor restart event records a
generic compose failure instead of the launch-time structured reason code.

Scope is limited to `resume_pr_monitor` companion env-secret recovery behavior
and focused unit coverage. Do not change protected workflow/configuration files
or run broad AWF/GitHub validation.

## Requirements Checklist

- Add a regression test proving missing required companion env secret sources on
  monitor resume record `COMPANION_ENV_SECRET_SOURCE_MISSING`, not
  `COMPOSE_COMMAND_FAILED`.
- Add coverage for empty required companion env secret sources recording
  `COMPANION_ENV_SECRET_SOURCE_EMPTY`.
- Preflight required env-backed companion secret refs during monitor resume
  before `ensure_project_up`.
- Preserve existing optional companion env-secret refresh behavior and avoid
  logging raw secret values.
- Keep monitor-resume behavior non-terminal: compose restart failures should
  still be recorded and the PR monitor should continue as existing tests expect.

## Implementation Steps

1. Add focused tests in the existing monitor recovery error-path test module.
2. Confirm the new regression fails before implementation when practical.
3. Add a small monitor-handoff helper that checks required env-backed companion
   refs against the resume process environment and raises `ComposeOperationError`
   with the launch-time structured reason code when missing or empty.
4. Call the helper immediately before optional-secret refresh and
   `ensure_project_up` in `resume_pr_monitor`.
5. Run focused tests for the affected module/test names only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "required_companion_env_secret or optional_companion_env_secret or compose_failure_records_warning"`
  - Passes with the new missing/empty required-secret tests and relevant existing
    resume behavior.
- Full AWF/GitHub validation is intentionally not executed during the agent
  phase; AWF owns broad validation after completion.
