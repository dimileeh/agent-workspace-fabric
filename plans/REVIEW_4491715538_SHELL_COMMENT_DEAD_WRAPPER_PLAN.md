# Review 4491715538 Shell Comment And Dead Wrapper Plan

## Problem Statement and Scope

Address the review-level comment for PR comment `issue:4491715538`.

The comment reports two issues:

- Informational workflow `run:` scripts with shell comment lines should remain
  allowed when the executable commands are safe.
- `WorkspaceExecutor._git_show_text` is unreachable dead code because staged
  protected-file diff loading calls the shared `git_show_text` helper directly.

Current inspection shows the shell-comment behavior already has a regression
test and the tokenizer no longer disables shell comments. Scope is therefore
limited to preserving and validating that behavior, removing the dead executor
wrapper, and moving its edge-case assertions to the shared helper tests.

## Requirements Checklist

- Verify shell comment lines in informational PR-comment steps remain allowed.
- Remove the unused `WorkspaceExecutor._git_show_text` method.
- Preserve missing-path and unexpected-error coverage for the shared
  `git_show_text` helper.
- Keep changes scoped to the reviewed files and their tests.
- Commit locally on the existing AWF branch without pushing or switching
  branches.

## Implementation Steps

1. Run the existing shell-comment regression to confirm current behavior.
2. Move executor-wrapper edge-case tests to `tests/unit/control/test_protected_file_diffs.py`.
3. Remove `WorkspaceExecutor._git_show_text`.
4. Run focused tests for shell comments, protected-file diff helpers, and the
   affected executor coverage area.
5. Run focused lint for the changed files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_continue_on_error_allows_shell_comments -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q -k protected_file_diffs_for_staged_paths`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py`
  passes.
