# Review PRRT_kwDOSJAM6s6F8TtL Cursor Failure Model Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6F8TtL_cursor_failure_model_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a lower-effort Cursor failure
  without an explicit model does not report `sonnet-4-thinking` in recovery
  metadata.
- Complete: Preserved explicit Cursor model override attribution with a paired
  failure-metadata regression.
- Complete: Preserved non-Cursor default-model attribution through the base
  hook's default implementation and the existing adapter unit coverage.
- Complete: Kept validation focused; full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/adapters/base.py`
- `src/awf/adapters/cursor.py`
- `tests/unit/adapters/test_adapters.py`
- `plans/review_PRRT_kwDOSJAM6s6F8TtL_cursor_failure_model_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6F8TtL_cursor_failure_model_VALIDATION.md`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k lower_effort_failure_metadata_omits_unselected_thinking_model`
- Result: failed because `exc.value.details["model"]` was
  `sonnet-4-thinking` instead of `unknown`.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k lower_effort_failure_metadata_omits_unselected_thinking_model`
- Result: passed, `1 passed, 50 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k cursor`
- Result: passed, `10 passed, 42 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q`
- Result: passed, `52 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/base.py src/awf/adapters/cursor.py tests/unit/adapters/test_adapters.py`
- Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/adapters/base.py src/awf/adapters/cursor.py`
- Result: passed.

No gaps remain in the saved plan. Broad AWF/GitHub-owned validation was not
run inside the agent phase per the workspace contract.
