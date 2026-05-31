# Monitor Handoff Ownership Repair Validation

Plan reference:
`COMMENT_3330714584_MONITOR_HANDOFF_OWNERSHIP_PLAN.md`

## Requirement Status

- Add runtime ownership repair before monitor handoff profile setup phases run:
  Complete.
- Use the same ownership repair reason code and executor event name as the
  normal executor setup path: Complete.
- If ownership repair fails, mark the workspace failed as infrastructure
  failure and do not run profile setup or the monitor: Complete.
- Preserve existing setup failure, exception, and dependency-network behavior:
  Complete.
- Add focused unit regression coverage: Complete.

## Evidence

Changed files:

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/COMMENT_3330714584_MONITOR_HANDOFF_OWNERSHIP_PLAN.md`
- `plans/COMMENT_3330714584_MONITOR_HANDOFF_OWNERSHIP_VALIDATION.md`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  - Pass: `5 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
  - Pass: `All checks passed!`

Full AWF/GitHub validation was not run locally because the workspace contract
assigns broad validation, provenance, and merge gating to AWF after agent
completion.
