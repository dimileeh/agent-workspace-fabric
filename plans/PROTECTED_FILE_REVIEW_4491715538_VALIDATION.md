# Protected File Review 4491715538 Validation

Plan reference: `PROTECTED_FILE_REVIEW_4491715538_PLAN.md`

## Requirement Status

- Coverage `fail_under` raise wording: Complete.
  - Evidence: `src/awf/control/quality_gates.py` now reports
    `coverage fail_under raised ... (policy change requires ownership of
    pyproject.toml)`.
  - Regression: `test_pyproject_raising_coverage_fail_under_is_blocked_with_explicit_policy_reason`.

- Preserve and document blocked new top-level `[dependency-groups]`: Complete.
  - Evidence: existing blocking behavior and regression test are unchanged.
    `docs/PROTECTED_FILES.md` now distinguishes adding dependency strings to
    existing groups from creating new top-level groups.

- PEP 735 include-group messaging: Complete.
  - Evidence: `src/awf/control/quality_gates.py` now detects list entries with
    `include-group` and reports that ownership is required for evaluation.
  - Regression: `test_pyproject_changed_pep735_include_group_reports_evaluation_limit`.

- Locale-independent protected file content loading: Complete.
  - Evidence: `src/awf/control/protected_file_diffs.py` now uses
    `git cat-file -e` before `git show`, verifies the base commit for missing
    paths, and raises for invalid refs or post-precheck `git show` failures.
  - Regression: updated `test_git_show_text_returns_none_for_missing_path`,
    `test_git_show_text_raises_for_unexpected_git_error`, and
    `test_git_show_text_raises_when_show_fails_after_object_precheck`.

- Focused regression tests before implementation: Complete.
  - Evidence: the first targeted run failed with the expected six failures, then
    passed after implementation.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py -q`
  - Result: Passed, `228 passed in 2.90s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py`
  - Result: Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: Passed, no issues in 158 source files.

## Gaps

None.
