# PRRT_kwDOSJAM6s6K9eKd Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K9eKd_PLAN.md`

## Requirement Status

- Add a regression test for a mirror-level `includeIf.gitdir` that is only
  visible from an attached worktree context: Complete.
- Repair poisoned hook config/includes exposed through the shared mirror config
  when evaluated from active linked worktrees: Complete.
- Keep existing direct mirror and per-worktree repair behavior intact: Complete.
- Run only focused local checks; broad AWF/GitHub validation remains managed by
  AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_repair.py`

Focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_removes_mirror_gitdir_include_exposed_from_worktree_context -q`
  - Failed before the implementation change with `assert False is True`.
  - Passed after the implementation change.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passed: 19 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Passed.

Full AWF/GitHub validation was not run inside the agent phase, per the
workspace contract. AWF owns broad validation, provenance, logs, and merge
gating after agent completion.
