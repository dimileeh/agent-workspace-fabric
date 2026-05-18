# PRRT_kwDOSJAM6s6C-9wz Test Path Work Phrases Validation

Plan reference: `PRRT_kwDOSJAM6s6C-9wz_TEST_PATH_WORK_PHRASES_PLAN.md`

## Requirement Status

- Complete: Added a regression test for `add assertions in tests/unit/...` in
  a mixed validation handoff gap.
- Complete: Preserved acceptance of validation-command handoffs that list test
  paths as command arguments.
- Complete: Updated `_has_test_path_work_context` to recognize bounded
  work-object phrases before test paths.
- Complete: Ran the narrow selection and the full planning unit test file.

## Evidence

- Changed `tests/unit/runtime/test_planning.py`.
- Changed `src/awf/runtime/planning.py`.
- Added this plan validation document.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q -k "named_command_handoff_with_paths or mixed_named_command_test_path_work_gaps"`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q -k "named_command_handoff_with_paths or mixed_named_command_test_path_work_gaps"`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`.
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`.

## Gaps

None.
