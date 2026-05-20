# Review 4491715538 Dependency/Coverage/Git-Show Validation

Plan reference: `plans/REVIEW_4491715538_DEP_COV_GITSHOW_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving a dotted dependency name is no
  longer classified as removed when the same normalized package remains under a
  dashed spelling.
- Complete: Added regression coverage proving an informational workflow
  run-command update using a `COV` shell variable is allowed.
- Complete: Existing `coverage` validation command detection remains covered by
  `test_workflow_comment_step_new_validation_command_is_blocked`.
- Complete: Added regression coverage proving PR monitor `_git_show_text`
  returns `None` for expected path-missing git-show errors.
- Complete: Added regression coverage proving PR monitor `_git_show_text`
  raises `RuntimeError` with refspec and git stderr details for unexpected
  git-show failures.
- Complete: The fix is local to the existing AWF branch; no branch switch or
  push was performed.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/control/test_quality_gates.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/REVIEW_4491715538_DEP_COV_GITSHOW_PLAN.md`
- `plans/REVIEW_4491715538_DEP_COV_GITSHOW_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "pep503 or cov_shell_variable or comment_step_new_validation_command"`
  - Result before implementation: failed for the PEP 503 dependency
    normalization and `COV` shell-variable regressions; the real coverage
    command blocking regression passed.
  - Result after implementation: passed, 3 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k "git_show_text"`
  - Result before implementation: failed because unexpected git-show errors did
    not raise `RuntimeError`; the existing success/path-missing cases passed.
  - Result after implementation: passed, 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py -q`
  - Result: passed, 190 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py src/awf/runtime/pr_monitor_runner.py`
  - Result: passed.

No gaps remain.
