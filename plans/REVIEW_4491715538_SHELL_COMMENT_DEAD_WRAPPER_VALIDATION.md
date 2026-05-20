# Review 4491715538 Shell Comment And Dead Wrapper Validation

Plan reference: `plans/REVIEW_4491715538_SHELL_COMMENT_DEAD_WRAPPER_PLAN.md`

## Requirement Status

- Complete: Shell comment lines in informational PR-comment steps remain
  allowed; the existing regression passed unchanged.
- Complete: Removed the unused `WorkspaceExecutor._git_show_text` method.
- Complete: Preserved missing-path and unexpected-error coverage on the shared
  `git_show_text` helper in `tests/unit/control/test_protected_file_diffs.py`.
- Complete: Changes are scoped to the executor helper cleanup, shared helper
  tests, and plan/validation files.
- Complete: Work is ready for local commit on the existing AWF branch.

## Evidence

Changed files:

- `src/awf/control/executor.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `plans/REVIEW_4491715538_SHELL_COMMENT_DEAD_WRAPPER_PLAN.md`
- `plans/REVIEW_4491715538_SHELL_COMMENT_DEAD_WRAPPER_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_continue_on_error_allows_shell_comments -q`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
  - Result: passed, 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_staged_protected_file_diffs_use_base_ref_for_old_side -q`
  - Result: passed.
- `rg -n "def _git_show_text|_git_show_text\(" src/awf tests`
  - Result: no matches.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py`
  - Result: passed.

No gaps remain.
