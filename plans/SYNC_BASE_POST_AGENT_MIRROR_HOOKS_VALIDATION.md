# Sync-Base Post-Agent Mirror Hooks Validation

Plan reference: `plans/SYNC_BASE_POST_AGENT_MIRROR_HOOKS_PLAN.md`

## Requirement Status

- Verify the reported path against current code before changing behavior:
  Complete. The sync-base post-agent plumbing exception handler logged
  `repair_mirror_hooks_path()` failures and re-raised the original exception.
- Add a regression test proving failed post-agent mirror-hooks repair raises the
  mirror-hooks failure instead of the original cleanup/plumbing exception:
  Complete. Added
  `test_run_sync_base_fails_closed_when_post_agent_mirror_hooks_repair_fails`.
- Change only the sync-base post-agent mirror-hooks repair failure behavior:
  Complete. Updated the targeted handler in `remote_ops.py` to raise
  `_MonitorMirrorHooksPathRepairFailedError` from the repair exception.
- Run targeted tests for the changed behavior only:
  Complete. Focused tests and lint passed.
- Record validation evidence and note that broad AWF/GitHub validation is
  managed after agent completion:
  Complete. Evidence is listed below. Full AWF/GitHub validation was not run in
  the agent phase per workspace contract.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_fails_closed_when_post_agent_mirror_hooks_repair_fails -q`
  initially failed before the production change, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_fails_closed_when_post_agent_mirror_hooks_repair_fails tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_repairs_mirror_hooks_after_conflict_agent_cleanup_failure -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  passed: 11 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`
  passed.
