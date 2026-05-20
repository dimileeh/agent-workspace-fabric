# Merge Development Conflict Resolution Plan

## Problem statement and scope

Resolve the in-progress `origin/development` merge conflicts on the current
AWF-managed branch without switching branches or pushing. Scope is limited to
the conflicted files reported by Git:

- `src/awf/service/bootstrap.py`
- `src/awf/service/config.py`
- `tests/integration/test_local_service_compose.py`
- `tests/unit/service/test_config.py`

## Requirements checklist

- Preserve the intent of both the PR branch and `origin/development`.
- Prefer base-branch semantics where a hunk cannot be reconciled confidently.
- Remove all conflict markers from the conflicted files.
- Keep unrelated existing worktree changes intact.
- Run the narrowest useful validation for the touched service config/bootstrap
  behavior.
- Stage the resolved files and commit locally with a conventional merge message.

## Implementation steps

1. Inspect conflict markers and surrounding code in all conflicted files.
2. Compare local and base-side semantics for each hunk.
3. Edit the conflicted files to combine compatible behavior.
4. Run targeted tests for service config/bootstrap and local compose behavior.
5. Create a validation document recording requirement status and evidence.
6. Stage all conflict-resolution files and commit locally.

## Verification commands and pass criteria

- `rg -n "<<<<<<<|=======|>>>>>>>" src/awf/service/bootstrap.py src/awf/service/config.py tests/integration/test_local_service_compose.py tests/unit/service/test_config.py`
  returns no matches.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/service/test_bootstrap.py tests/integration/test_local_service_compose.py -q`
  passes, or any failure is documented if caused by unavailable workspace
  infrastructure rather than the merge resolution.
