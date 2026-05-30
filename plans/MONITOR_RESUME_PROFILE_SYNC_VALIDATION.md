# Monitor Resume Profile Sync Validation

Plan reference: `plans/MONITOR_RESUME_PROFILE_SYNC_PLAN.md`

## Requirement Status

- Regression test for failed first `_sync_resolved_profile` retry: Complete.
  Added `test_resume_pr_monitor_retries_profile_sync_before_monitor_factory`.
- Monitor factory receives persisted/synced profile: Complete. The regression
  asserts the factory receives `db-synced` after the second sync call writes the
  workspace snapshot.
- Compose restart behavior unchanged: Complete. The code change only prevents
  an unsynced local profile from being assigned to `profile`; the existing
  non-terminal timeout-resolution exception path is preserved.
- Avoid broad validation: Complete. Only targeted unit tests and focused Ruff
  checks were run; full AWF/GitHub validation remains managed by AWF after
  agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py`
- `plans/MONITOR_RESUME_PROFILE_SYNC_PLAN.md`
- `plans/MONITOR_RESUME_PROFILE_SYNC_VALIDATION.md`

Commands:

- Initial TDD failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q -k retries_profile_sync`
  failed because `sync_attempts` contained only one entry.
- Targeted regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q -k retries_profile_sync`
  passed.
- Nearby focused unit coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py -q`
  passed: 17 passed.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py`
  passed.

## Remaining Gaps

None for the planned scope.
