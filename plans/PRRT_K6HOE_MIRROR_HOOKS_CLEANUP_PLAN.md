# PRRT_K6HOE Mirror Hooks Cleanup Plan

## Problem Statement And Scope

The PR monitor review-fix agent repairs the shared mirror `core.hooksPath` before
launching the agent, but if the adapter raises a cleanup or other non-`AgentRunError`
exception during the agent invocation, the function exits before the dirty-worktree
commit path. That can leave a mirror hook override written by the agent in place for
sibling workspaces.

Scope is limited to `src/awf/runtime/pr_monitor_runner/comments.py` and a focused
unit regression for the review-fix agent path.

## Requirements Checklist

- Add a regression test showing that a non-`AgentRunError` from `adapter.run` triggers
  a second mirror hooks repair before the original exception is propagated.
- Keep the existing pre-launch mirror repair behavior unchanged, including blocking
  agent launch when pre-launch repair fails.
- Do not broaden exception handling into verdict parsing or dirty-worktree commit
  behavior.
- Run only focused validation for the touched test.

## Implementation Steps

1. Add a unit test beside the existing comment-agent mirror hooks tests.
2. Confirm the new test fails against the current implementation when practical.
3. Wrap only the adapter invocation in a cleanup path that reruns
   `repair_mirror_hooks_path()` when a mirror exists.
4. Preserve existing logging and `_MonitorMirrorHooksPathRepairFailedError` behavior
   if the repair itself fails.
5. Run the focused unit test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q -k "invoke_cli_for_verdict_result"`

Pass criteria: the focused tests pass, including the new non-`AgentRunError` regression.
Full AWF/GitHub validation remains managed by AWF after agent completion.
