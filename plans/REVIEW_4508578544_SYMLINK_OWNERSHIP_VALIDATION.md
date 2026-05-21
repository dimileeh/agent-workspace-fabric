# Review 4508578544 Symlink Ownership Validation

Plan reference: `plans/REVIEW_4508578544_SYMLINK_OWNERSHIP_PLAN.md`

## Requirement Status

- Add a regression test showing a valid linked worktree whose `.git` metadata
  points through a symlinked mirror prefix is accepted: Complete.
- Preserve existing safety checks that reject mirrors outside the expected AWF
  mirror root or metadata for another workspace: Complete.
- Fix the parent comparison so equivalent resolved paths compare equal:
  Complete.
- Keep the change narrow and commit it locally without pushing: Complete.

## Evidence

- Changed `tests/unit/runtime/test_ownership.py` to add
  `test_repair_agent_runtime_ownership_allows_symlinked_mirror_prefix`.
- Confirmed the new regression failed before the implementation change:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_allows_symlinked_mirror_prefix -q`
  failed because `ok` was `False`.
- Changed `src/awf/runtime/ownership.py` so the linked-worktree metadata parent
  is resolved before comparison with the resolved expected parent.
- Verified after the fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  passed with `7 passed`.
- Verified lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`
  passed.
