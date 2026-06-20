## Plan Reference

- `plans/PRRT_kwDOSJAM6s6K11vI_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing mirror hooks repair ignores
  inherited Git object lookup environment overrides.
- Complete: Applied the existing Git object lookup env cleanup to both
  `git config` subprocesses in `repair_mirror_hooks_path`.
- Complete: Preserved existing repair, no-op, and error behavior in the
  focused hooks repair and HEAD object verification tests.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager.py`
- `plans/PRRT_kwDOSJAM6s6K11vI_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K11vI_VALIDATION.md`

Focused checks:

- Before implementation, the new regression failed with
  `GitOperationError: git mirror.hooks_path_probe failed (exit=128, reason=MIRROR_HOOKS_PATH_REPAIR_FAILED): fatal: --local can only be used inside a git repository`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k ignores_git_object_lookup_envs_for_config_repair`
  passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k 'RepairMirrorHooksPath or VerifyHeadObjectExists'`
  passed with `9 passed, 40 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
