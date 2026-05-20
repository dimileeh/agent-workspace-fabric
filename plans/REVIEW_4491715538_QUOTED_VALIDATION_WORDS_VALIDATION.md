# Review 4491715538 Quoted Validation Words Validation

Plan reference: `plans/REVIEW_4491715538_QUOTED_VALIDATION_WORDS_PLAN.md`

## Requirement Status

- Complete: Safe informational `echo` and `printf` steps with quoted
  validation-tool output are allowed. Evidence:
  `test_workflow_comment_continue_on_error_allows_quoted_validation_words` and
  `test_added_informational_step_allows_quoted_validation_words`.
- Complete: Real validation commands remain blocked. Evidence: the focused
  regression run included existing blockers for new validation commands,
  validation-named comment steps, and validation `continue-on-error` changes.
- Complete: `_is_validation_command` now evaluates shell command words rather
  than applying validation-command regexes to quoted string arguments.
- Complete: `_shell_tokens` no longer sets `lexer.whitespace_split = True`; it
  explicitly keeps shell expansion token forms intact so existing secret and
  command-substitution safety checks remain effective.
- Complete: Scope stayed limited to the quality-gate classifier, focused unit
  regressions, and this plan/validation pair.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'quoted_validation_words or real_validation_commands or comment_step_new_validation_command or pytest_continue_on_error or named_validation_continue_on_error'`
  passed with 8 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with 230 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
