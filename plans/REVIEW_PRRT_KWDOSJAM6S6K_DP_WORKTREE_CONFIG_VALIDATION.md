# Review PRRT_kwDOSJAM6s6K-dP- Worktree Config Validation

Plan reference:
`plans/REVIEW_PRRT_KWDOSJAM6S6K_DP_WORKTREE_CONFIG_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a linked worktree with a stray
  `config.worktree` while `extensions.worktreeConfig` is unset.
- Complete: Preserved existing repair behavior when `extensions.worktreeConfig=true`;
  the existing worktree-config repair tests still pass.
- Complete: Skipped ignored `config.worktree` files before invoking
  `git config --worktree`.
- Complete: Kept validation focused; full AWF/GitHub validation is managed by
  AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_repair.py`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K_DP_WORKTREE_CONFIG_PLAN.md`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K_DP_WORKTREE_CONFIG_VALIDATION.md`

Focused checks:

- Expected pre-fix failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_ignores_config_worktree_when_worktree_config_extension_is_disabled -q`
  failed with Git's `--worktree`/`extensions.worktreeConfig` error.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_ignores_config_worktree_when_worktree_config_extension_is_disabled tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_linked_worktree_config_probes_include_safe_directory -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  passed: 28 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  passed.

## Gaps

None.
