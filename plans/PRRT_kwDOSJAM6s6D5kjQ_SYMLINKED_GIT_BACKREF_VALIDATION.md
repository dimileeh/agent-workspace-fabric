# PRRT_kwDOSJAM6s6D5kjQ Symlinked Git Backref Validation

Plan reference: `PRRT_kwDOSJAM6s6D5kjQ_SYMLINKED_GIT_BACKREF_PLAN.md`

## Requirement Status

- Complete: Added a regression test demonstrating that a symlinked workspace
  `.git` back-reference is rejected and ownership repair is not called.
- Complete: Preserved valid numeric-suffix linked worktree behavior; the full
  ownership unit test file passes.
- Complete: Updated `_validate_linked_git_dir_backref` to reject a workspace
  `.git` endpoint unless it is a non-symlink file before resolving paths for the
  reciprocal metadata comparison.
- Complete: Validated with the focused ownership unit tests, ruff, and mypy.

## Evidence

Files changed:

- `src/awf/runtime/ownership.py`
- `tests/unit/runtime/test_ownership.py`
- `plans/PRRT_kwDOSJAM6s6D5kjQ_SYMLINKED_GIT_BACKREF_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6D5kjQ_SYMLINKED_GIT_BACKREF_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_blocks_symlinked_git_backref -q`
  - Failed before the implementation with `assert True is False`.
  - Passed after the implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  - Passed: `14 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/ownership.py`
  - Passed.

## Gaps

None.
