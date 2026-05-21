# Comment 3282646309 Plan

## Problem Statement And Scope

The runtime ownership repair guard accepts linked-worktree Git metadata whose
admin directory starts with `workspace_id` and has a numeric suffix. That lets a
workspace such as `ws_1` trust metadata for `ws_12`, which can send ownership
repair into another workspace's Git metadata under the same mirror.

Scope is limited to the runtime ownership validation path and its unit
regressions for PR review thread `PRRT_kwDOSJAM6s6D3KyS`.

## Requirements Checklist

- Add a failing regression proving `workspace_id="ws_1"` rejects linked Git
  metadata named `ws_12`.
- Tighten linked-worktree metadata validation so trusted metadata maps exactly
  to the current workspace identifier.
- Preserve existing mirror-root, linked-parent, symlinked mirror, and
  wrong-workspace rejection behavior.
- Run focused validation for `tests/unit/runtime/test_ownership.py`.

## Implementation Steps

1. Add the prefix-collision regression test beside the existing ownership repair
   safety tests.
2. Run the focused test to confirm the new regression fails against current
   behavior.
3. Replace the prefix-plus-numeric-suffix check with exact workspace-id matching.
4. Update any obsolete test expectation that encoded the unsafe suffix policy.
5. Re-run the focused test file and relevant lint for changed files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`

Pass criteria: the focused runtime ownership tests pass, and lint reports no
issues for the changed Python files.
