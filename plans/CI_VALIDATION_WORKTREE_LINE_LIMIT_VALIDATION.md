# CI Validation Worktree Line Limit Validation

Plan reference: `plans/CI_VALIDATION_WORKTREE_LINE_LIMIT_PLAN.md`

## Requirement Status

- Complete: Existing validation worktree behavior remains covered.
  - Moved the tail cleanup edge tests to
    `tests/unit/runtime/test_validation_worktree_cleanup_edges.py` without
    changing assertions.
  - Focused validation passed for the original and split test modules.
- Complete: Every first-party code file is at most 1500 lines.
  - `tests/unit/runtime/test_validation_worktree.py` is now 1240 lines.
  - `tests/unit/runtime/test_validation_worktree_cleanup_edges.py` is 301
    lines.
  - The exact failing maintainability guard now passes.
- Complete: No protected workflow, quality-gate, or configuration files were
  edited.
  - Changes are limited to test organization and required plan/validation
    documents.
- Complete: Broad AWF/GitHub-owned validation was not run locally.
  - Only focused checks listed below were executed.
- Complete: Scoped CI fix is ready for local commit.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `1 passed in 0.52s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_cleanup_edges.py -q`
  - Passed: `46 passed in 1.81s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_cleanup_edges.py -q`
  - Passed: `7 passed in 0.53s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_cleanup_edges.py`
  - Passed: `All checks passed!`.
- `git diff --check`
  - Passed with no output.

Full AWF/GitHub validation remains managed by AWF after agent completion.
