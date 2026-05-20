# Review 4491715538 Dependency/Coverage/Git-Show Plan

## Problem Statement and Scope

Address the current actionable review feedback for PR review-level comment
`issue:4491715538`.

The review flags three targeted correctness issues in the protected
quality-gate implementation:

- Python dependency names should follow PEP 503/685 normalization, treating
  `-`, `_`, and `.` runs as equivalent.
- Workflow validation-command detection should not treat a bare `cov` variable
  or shell token as the coverage CLI.
- PR monitor `git show` reads should distinguish expected path-missing cases
  from unexpected git failures, matching the executor helper.

Scope is limited to the quality-gate classifier, PR monitor git-show helper,
focused regression tests, and this plan/validation documentation.

## Requirements Checklist

- Add regression coverage proving dotted dependency names are not classified as
  removed when the same normalized package remains under a dash/underscore form.
- Add regression coverage proving informational workflow run-command changes
  using a `COV` shell variable are allowed.
- Preserve detection and blocking for real `coverage`, `pytest`, `ruff`, and
  `mypy` validation commands.
- Add regression coverage proving PR monitor `_git_show_text` returns `None`
  for expected path-missing git-show errors.
- Add regression coverage proving PR monitor `_git_show_text` raises
  `RuntimeError` with diagnostic details for unexpected git-show failures.
- Commit the fix locally on the existing AWF branch without pushing or changing
  branches.

## Implementation Steps

1. Add focused failing tests in:
   - `tests/unit/control/test_quality_gates.py`
   - `tests/unit/runtime/test_pr_monitor_runner.py`
2. Run the focused tests before implementation and confirm failures for the new
   regression cases where practical.
3. Update `_dependency_name` to normalize `[-_.]+` to `-`.
4. Remove the broad `cov` token from `_VALIDATION_COMMAND_TOKEN_RE` while
   keeping `coverage` detection.
5. Update `PullRequestMonitorRunner._git_show_text` to return `None` only for
   expected path-missing errors and raise for unexpected git failures.
6. Run focused tests, then the affected unit modules and static checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "pep503 or cov_shell_variable or comment_step_new_validation_command"`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k "git_show_text"`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py src/awf/runtime/pr_monitor_runner.py`
  passes.
