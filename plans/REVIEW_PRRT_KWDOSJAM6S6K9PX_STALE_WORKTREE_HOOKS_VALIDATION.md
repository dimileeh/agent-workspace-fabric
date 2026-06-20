# Stale Worktree Hooks Repair Review Validation

Plan reference:
`plans/REVIEW_PRRT_KWDOSJAM6S6K9PX_STALE_WORKTREE_HOOKS_PLAN.md`

## Requirement Status

- Verify the review claim against local code and existing tests: Complete.
  `repair_mirror_hooks_path` resolved every linked-worktree back-reference and
  called `git -C <worktree>` without checking whether the linked worktree still
  existed.
- Add focused regression coverage for stale linked-worktree metadata left under
  a mirror's `worktrees` directory: Complete.
  Added `test_skips_stale_linked_worktree_entry` in
  `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
- Preserve repair behavior for existing linked worktrees and mirror config:
  Complete. The repair loop still probes existing linked worktrees and
  `config.worktree`; it only skips resolved paths that no longer exist.
- Avoid broad AWF/GitHub-owned validation: Complete.
  Only focused tests and lint for touched files were run locally. Full
  AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_repair.py`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K9PX_STALE_WORKTREE_HOOKS_PLAN.md`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K9PX_STALE_WORKTREE_HOOKS_VALIDATION.md`

Focused red check before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_skips_stale_linked_worktree_entry -q`
  - Failed as expected with `GitOperationError` from `git -C <missing>`.

Focused verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_skips_stale_linked_worktree_entry -q`
  - Passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passed: 20 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Passed.

## Gaps

None.
