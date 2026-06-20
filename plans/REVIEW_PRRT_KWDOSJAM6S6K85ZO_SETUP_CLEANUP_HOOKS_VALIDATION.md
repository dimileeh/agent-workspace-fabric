# Review PRRT_kwDOSJAM6s6K85zO Setup Cleanup Hooks Validation

Plan reference:
`REVIEW_PRRT_KWDOSJAM6S6K85ZO_SETUP_CLEANUP_HOOKS_PLAN.md`

## Requirement Status

- Confirm the setup/pre-agent cleanup-error path currently bypasses post-setup
  mirror hooks repair: Complete.
  - The new regression failed before the code change with only one mirror
    repair call recorded.
- Add a regression test covering `run_profile_phases(... setup/pre_agent ...)`
  raising `ComposeExecCleanupError`: Complete.
  - Added
    `test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure`.
- Ensure the executor attempts mirror hooks repair after that setup cleanup
  failure before marking the workspace failed: Complete.
  - `execution_flow.execute` now repairs after profile setup cleanup failure
    and re-raises to the existing cleanup handler.
- Preserve existing cleanup-failure classification and failure message behavior:
  Complete.
  - The regression asserts `EXEC_PROCESS_CLEANUP_FAILED` and the existing
    cleanup failure message.
- Keep validation focused; full AWF/GitHub validation remains managed after the
  agent exits: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/execution_flow.py`
- `src/awf/control/executor/mirror_hooks_repair.py`
- `tests/unit/control/test_executor_mirror_hooks_path.py`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K85ZO_SETUP_CLEANUP_HOOKS_PLAN.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -k setup_cleanup_failure -q`
  - Before implementation: failed because `repair_calls` contained only the
    pre-setup repair.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Passed: 13 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py src/awf/control/executor/mirror_hooks_repair.py tests/unit/control/test_executor_mirror_hooks_path.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_flow.py src/awf/control/executor/mirror_hooks_repair.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation and merge gating after completion.
