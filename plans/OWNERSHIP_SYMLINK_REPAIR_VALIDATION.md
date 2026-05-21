# Ownership Symlink Repair Validation

Plan reference: `OWNERSHIP_SYMLINK_REPAIR_PLAN.md`

## Requirement Status

- Add a regression test proving non-recursive chown targets use `os.lchown`
  for symlink entries: Complete. Added
  `test_chown_targets_uses_lchown_for_non_recursive_symlink`.
- Preserve existing recursive chown behavior and object-directory exceptions:
  Complete. Existing node ownership tests pass unchanged.
- Keep the fix in the shared helper used by executor and monitor repair paths:
  Complete. `_chown_targets` now handles non-recursive symlink entries with
  `os.lchown`.
- Run the narrow affected test surface: Complete. See evidence below.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::test_chown_targets_uses_lchown_for_non_recursive_symlink -q`
  - Failed before the implementation because `os.chown` received the symlink.
  - Passed after the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/runtime/test_ownership.py -q`
  - Passed: 40 tests.

## Remaining Gaps

None.
