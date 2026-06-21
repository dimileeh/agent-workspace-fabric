# PR614 CI Post-Agent Mirror Repair Validation

Plan reference: `plans/PR614_CI_POST_AGENT_MIRROR_REPAIR_PLAN.md`

## Requirement Status

- Update the focused regression so a failed post-agent mirror hooks repair
  reports `MIRROR_HOOKS_PATH_POISONED`: Complete.
- Preserve existing behavior when post-agent mirror repair succeeds by
  re-raising the original adapter/plumbing exception: Complete.
- Keep the change minimal and avoid broad validation in the agent phase:
  Complete.

## Evidence

- Changed `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  to expect `_MonitorMirrorHooksPathRepairFailedError` when the second
  CI-fix mirror hooks repair fails, while still expecting
  `ComposeExecCleanupError` when that repair succeeds.
- Changed `src/awf/runtime/pr_monitor_runner/ci_ops.py` so the failed
  post-agent mirror repair raises `_MonitorMirrorHooksPathRepairFailedError`
  chained from the repair exception.
- Confirmed the updated focused regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
  failed in the `post_repair_fails=True` case because `ComposeExecCleanupError`
  was still re-raised.
- Confirmed the focused regression passes after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
  passed with `2 passed, 16 deselected`.
- Confirmed focused lint passes:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase, per workspace
contract; AWF/GitHub own broad validation, provenance, logs, and merge gating
after agent completion.
