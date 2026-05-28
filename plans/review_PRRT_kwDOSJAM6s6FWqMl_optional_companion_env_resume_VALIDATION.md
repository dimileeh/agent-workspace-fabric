# Review PRRT_kwDOSJAM6s6FWqMl Optional Companion Env Resume Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6FWqMl_optional_companion_env_resume_PLAN.md`

## Requirement Status

- Preserve missing optional env-secret omission before `ensure_project_up`:
  Complete. Existing targeted test still passes.
- Restore present optional env-secret placeholders from task policy without raw
  secret values:
  Complete. Added a regression that starts from a persisted compose file missing
  the optional target and verifies resume restores `${OPTIONAL_TOKEN_SOURCE:-}`.
- Cover the missing-then-present resume case:
  Complete. Added
  `test_resume_pr_monitor_restores_present_optional_companion_env_secret_placeholder`.
- Keep validation focused:
  Complete. Ran only the affected unit selection and changed-file Ruff check.
  Full AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `plans/review_PRRT_kwDOSJAM6s6FWqMl_optional_companion_env_resume_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6FWqMl_optional_companion_env_resume_VALIDATION.md`

Focused checks:

- Pre-implementation regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -k "optional_companion_env_secret" -q`
  failed with the new restoration test missing `OPTIONAL_TOKEN`.
- Post-implementation behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -k "optional_companion_env_secret" -q`
  passed: `2 passed, 24 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py`
  passed.

## Gaps

None.
