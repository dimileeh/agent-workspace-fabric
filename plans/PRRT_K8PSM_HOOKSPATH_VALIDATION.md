# PRRT K8PSM HooksPath Repair Validation

Plan reference: `plans/PRRT_K8PSM_HOOKSPATH_PLAN.md`

## Requirement Status

- Add a regression test showing `repair_mirror_hooks_path` clears
  `core.hooksPath` from linked worktree config: Complete.
  - Added `test_clears_poisoned_worktree_local_hooks_path`.
  - Confirmed the new test failed before the implementation because the helper
    returned `False` while worktree-local `core.hooksPath` remained set.
- Preserve the existing mirror-local repair behavior and return semantics:
  Complete.
  - Focused mirror-hook repair suite passes with existing tests.
- Raise `GitOperationError` with existing repair reason code if worktree-local
  config repair fails: Complete.
  - Worktree config repair uses the same helper as mirror-local repair and the
    same `MIRROR_HOOKS_PATH_REPAIR_FAILED` reason code on probe, reprobe, and
    unset failures.
- Avoid broad validation; AWF/GitHub own full validation after agent
  completion: Complete.
  - Ran only focused tests and checks listed below.

## Evidence

- Files changed:
  - `src/awf/node/git_manager.py`
  - `tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - `plans/PRRT_K8PSM_HOOKSPATH_PLAN.md`
  - `plans/PRRT_K8PSM_HOOKSPATH_VALIDATION.md`
- Focused commands:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_clears_poisoned_worktree_local_hooks_path -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py`
  - `uv run --python 3.12 --extra dev mypy src/awf/node/git_manager.py`

All focused verification passed. Full AWF/GitHub validation is managed after
agent completion.
