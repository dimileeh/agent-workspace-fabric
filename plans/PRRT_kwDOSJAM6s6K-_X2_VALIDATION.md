# PRRT_kwDOSJAM6s6K-_X2 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K-_X2_PLAN.md`

## Requirement Status

- Complete: Added a focused regression test for an included config with multiple `core.hooksPath` values from the same origin.
- Complete: Preserved strict failure behavior for unmapped or unremovable included origins; the existing failure branches remain intact.
- Complete: `_repair_hooks_path_config` now avoids reprocessing an included origin after its include has already been removed.
- Complete: Ran focused local validation only. Full AWF/GitHub validation is managed by AWF after agent completion per workspace contract.

## Evidence

Files changed:

- `src/awf/node/git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_repair.py`
- `plans/PRRT_kwDOSJAM6s6K-_X2_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K-_X2_VALIDATION.md`

Failed-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q -k multiple_hooks_paths_from_same_included_origin`
  - Failed before implementation with `MIRROR_HOOKS_PATH_REPAIR_FAILED` from the second hook value in the same included origin.

Passing focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q -k "multiple_hooks_paths_from_same_included_origin or removes_include_exposing_poisoned_hooks_path"`
  - Passed: `2 passed, 27 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passed: `29 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Passed.

No broad coverage gate, whole-repository test suite, full frontend build, or CI-equivalent validation was run in the agent phase.
