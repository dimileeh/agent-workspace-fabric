# PR614 Shard 3 Cleanup Missing Head Validation

Plan reference: `plans/PR614_SHARD3_CLEANUP_MISSING_HEAD_PLAN.md`

## Requirement Status

- Complete: Did not switch branches, push, rebase, or run broad
  AWF/GitHub-owned validation.
- Complete: Reproduced the shard 3 failure locally before editing.
- Complete: Preserved mirror-hooks cleanup repair behavior in the agent
  `ComposeExecCleanupError` path.
- Complete: Added missing-HEAD verification after cleanup repair and before the
  existing cleanup-failure marker.
- Complete: If HEAD is missing, the path now invokes existing missing-HEAD
  recovery with stage `agent_run_cleanup_failure` and verifies the recovered
  post-agent commit.
- Complete: The existing outer cleanup-failure handling still marks
  `EXEC_PROCESS_CLEANUP_FAILED`, so the operator sees the original cleanup
  failure after recovery.
- Complete: Ran focused local verification only.
- Complete: Full AWF/GitHub validation, coverage gates, and CI provenance remain
  managed by AWF after agent completion.

## Files Changed

- `src/awf/control/executor/execution_flow.py`
- `plans/PR614_SHARD3_CLEANUP_MISSING_HEAD_PLAN.md`
- `plans/PR614_SHARD3_CLEANUP_MISSING_HEAD_VALIDATION.md`

## Evidence

- Failing focused repro before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure -q`
  failed because `verify_head_object_exists` was awaited 0 times.
- Passing focused test after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure -q`
  passed: `1 passed in 0.74s`.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_mirror_hooks_path_commit.py`
  passed: `All checks passed!`.

## Residual Risk

The current remote CI run is for the pre-fix PR head. AWF owns pushing these
local commits and running the full post-agent validation/provenance checks after
this agent phase.
