# PRRT_kwDOSJAM6s6K6NLB CI cleanup mirror repair validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6K6NLB_PLAN.md`

## Requirement status
- Complete: Added a regression test proving CI-fix adapter cleanup exceptions
  rerun mirror hook repair after the agent starts and before the original
  exception propagates.
- Complete: Kept the existing pre-launch mirror repair behavior unchanged.
- Complete: Preserved the original adapter exception even when the post-agent
  mirror repair itself fails; that repair failure is logged and not returned
  as the terminal failure.
- Complete: Ran only focused checks for the touched behavior. Full AWF/GitHub
  validation is managed after agent completion.

## Evidence
- Changed `src/awf/runtime/pr_monitor_runner/ci_ops.py` to repair the mirror
  in the non-`AgentRunError` adapter exception path and then re-raise the
  original exception.
- Changed
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  with a focused regression covering successful post-agent repair and failed
  post-agent repair that still preserves `ComposeExecCleanupError`.

## Verification
- Red check before the fix:
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
  failed with `assert ['repair', 'agent'] == ['repair', 'agent', 'repair']`.
- Green check after the fix:
  `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
  passed: `2 passed, 19 deselected`.
- Scoped lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  passed.
