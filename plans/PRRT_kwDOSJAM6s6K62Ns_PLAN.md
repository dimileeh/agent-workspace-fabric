# PRRT_kwDOSJAM6s6K62Ns Plan

## Problem Statement and Scope

The sync-base conflict repair path repairs a shared mirror hooks path before launching the conflict-resolution agent, but a non-`AgentRunError` from `adapter.run` exits before `_commit_dirty_worktree` can run its existing mirror guard. The review asks for the same post-agent mirror repair used by CI/comment/protected repair paths.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_ops.py` and a focused unit regression for `_run_sync_base`.

## Requirements Checklist

- Add a regression proving sync-base conflict repair re-repairs the mirror after a non-`AgentRunError` adapter failure.
- Preserve the original adapter/runtime exception after the repair attempt.
- Do not change clean sync-base, pre-launch repair, `AgentRunError`, protected-scope, or push behavior.
- Run only focused checks for the changed behavior; AWF/GitHub owns broad validation after this agent phase.

## Implementation Steps

1. Add a failing unit test in the existing sync-base regression module.
2. Update `_run_sync_base` to repair `mirror_path` in a broad adapter exception path before re-raising.
3. Run the targeted unit test module or narrow test selection.
4. Record verification evidence in the validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
- Pass criterion: the targeted sync-base regression module passes.
- Broad AWF/GitHub validation is intentionally not run in the agent phase per workspace contract.
