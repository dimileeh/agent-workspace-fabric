# Monitor Handoff Fallback Persistence Validation

Plan reference:
`plans/COMMENT_3331327765_MONITOR_HANDOFF_FALLBACK_PLAN.md`

## Requirement Status

- Add a regression test showing direct fallback persistence failures propagate:
  Complete. Added
  `test_mark_failed_from_monitor_handoff_setup_failure_reraises_direct_fallback_error`.
- Preserve successful direct fallback behavior:
  Complete. Existing `terminal_fallback` coverage passed.
- Preserve existing behavior when no direct persistence fallback is available:
  Complete. Existing no-session fallback coverage passed.
- Keep secret redaction and terminal failure payload shaping unchanged:
  Complete. Code change only re-raises after the existing fallback failure log.
- Use only focused validation:
  Complete. No broad AWF/GitHub validation suite was run; AWF/GitHub owns broad
  validation after agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/COMMENT_3331327765_MONITOR_HANDOFF_FALLBACK_PLAN.md`
- `plans/COMMENT_3331327765_MONITOR_HANDOFF_FALLBACK_VALIDATION.md`

Focused checks:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k reraises_direct_fallback_error`
  failed with `DID NOT RAISE <class 'RuntimeError'>`.
- Final targeted test:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k "monitor_handoff_setup_failure or terminal_fallback"`
  passed: `3 passed, 17 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
  passed.

## Remaining Gaps

None for this review-thread fix.
