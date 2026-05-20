# Review 4491715538 Fix Validation

Plan reference: `plans/REVIEW_4491715538_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proves informational `echo` and `printf`
  steps mentioning `build`, `lint`, `release`, `publish`, or `deploy` prose are
  allowed.
- Complete: Existing informational step run updates with harmless broad prose
  words are allowed when they do not introduce validation commands.
- Complete: Real broad commands such as `npm run build`,
  `npm --prefix apps/console run build`, `make lint`, `python -m build`,
  `gcloud run deploy`, and `npm publish` remain blocked.
- Complete: Unused `_git_diff_text` helpers were removed from executor and PR
  monitor runner code.
- Complete: Existing fail-closed protected-file behavior is preserved outside
  the targeted workflow classifier and dead-helper cleanup.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `src/awf/control/executor.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/control/test_quality_gates.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/REVIEW_4491715538_PLAN.md`
- `plans/REVIEW_4491715538_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'echo_prose_validation_words or existing_informational_step_allows_echo_prose_validation_word_update or real_broad_validation_commands'`
  - Result before implementation: failed with the expected five harmless-prose
    false-positive failures; the real-command cases still passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'real_broad_validation_commands'`
  - Result during implementation: failed for
    `npm --prefix apps/console run build`, confirming the preservation gap
    before package-manager option handling was added.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'echo_prose_validation_words or existing_informational_step_allows_echo_prose_validation_word_update or real_broad_validation_commands or comment_validation_command_broadening'`
  - Result: passed, 12 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Result: passed, 62 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k 'git_show_text_marks_worktree_safe_directory or changed_paths_between_ref_and_head_includes_rename_sources or protected_status_diff_for_deleted_file_keeps_head_text'`
  - Result: passed, 3 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py`
  - Result: passed.

No gaps remain.
