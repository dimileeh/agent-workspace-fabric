# PRRT_kwDOSJAM6s6K5LVO Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K5LVO_PLAN.md`

## Requirement Status

- Verify the review claim against current code: Complete. The sync-base conflict
  path called `_handle_provider_agent_run_error()` before
  `_commit_dirty_worktree()`.
- Add a focused regression test for the ordering: Complete. Added
  `test_run_sync_base_runs_post_agent_guard_before_provider_retry`.
- Run `_commit_dirty_worktree` before provider recovery can short-circuit:
  Complete. The sync-base conflict path now invokes the commit sink first.
- Preserve provider recovery propagation after the post-agent guard runs:
  Complete. The regression expects `ProviderRecoveryRetryError` to propagate
  after the sink records its call.
- Run targeted validation only: Complete. Full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`

Focused checks:

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_runs_post_agent_guard_before_provider_retry -q`
  failed because events were `["provider-recovery"]` instead of
  `["commit", "provider-recovery"]`.
- Final targeted checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_runs_post_agent_guard_before_provider_retry -q`
  passed.
- Focused module check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  passed with 7 tests.

No remaining planned gaps.
