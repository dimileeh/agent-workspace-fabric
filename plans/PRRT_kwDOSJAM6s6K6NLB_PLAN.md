# PRRT_kwDOSJAM6s6K6NLB CI cleanup mirror repair plan

## Problem statement and scope
Review thread `PRRT_kwDOSJAM6s6K6NLB` reports that `_run_ci_fix` repairs a
worktree mirror's `core.hooksPath` before launching the CI fix agent, but if
the adapter raises `ComposeExecCleanupError` or another non-`AgentRunError`,
control leaves before `_commit_dirty_worktree` can run its own mirror repair.
That can leave the shared mirror poisoned for sibling or future workspaces.

Scope is limited to `src/awf/runtime/pr_monitor_runner/ci_ops.py` and a
focused regression test for the CI-fix cleanup exception path.

## Requirements checklist
- [ ] Add a regression test proving CI-fix adapter cleanup exceptions rerun
      mirror hook repair after the agent starts and before the exception
      propagates.
- [ ] Keep the existing pre-launch mirror repair fail-closed behavior.
- [ ] Preserve the original adapter exception; a failed post-agent mirror
      repair must be logged but must not hide the cleanup failure.
- [ ] Run only focused checks for the touched behavior; AWF/GitHub own broad
      validation after agent completion.

## Implementation steps
1. Add the regression test in the existing mirror-poisoning test shard.
2. Run the new test and confirm it fails on the current code.
3. Add a narrow post-agent exception cleanup path around the CI-fix adapter run.
4. Re-run the focused test and lint the touched files.

## Verification commands
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`

## Pass criteria
- The new regression fails before the fix and passes after it.
- Focused lint passes on the source and test files touched.
