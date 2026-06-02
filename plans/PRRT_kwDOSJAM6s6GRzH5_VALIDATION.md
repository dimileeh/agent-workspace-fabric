# PRRT_kwDOSJAM6s6GRzH5 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GRzH5_PLAN.md`

## Requirement Status

- Add a regression test for successful destroy cleanup with `compose_file_path`
  present and `compose_project_name` absent: Complete.
- Confirm the regression test fails before the implementation change when
  practical: Complete.
- Emit `workspace.terminal_runtime_released` after successful cleanup when
  either runtime locator is present and the release event is not already
  recorded: Complete.
- Include useful release-event payload fields without assuming a compose project
  exists: Complete.
- Run focused validation for the touched unit test behavior: Complete.

## Evidence

- Changed `src/awf/service/controls.py` to gate destroy cleanup release events
  on either `compose_project_name` or `compose_file_path`, and to include
  `compose_file_path` in release payloads.
- Changed
  `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py`
  to cover compose-file-only destroy cleanup.
- Failing-before check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py::test_destroy_compose_file_only_records_runtime_released_after_cleanup -q`
  failed because `has_terminal_runtime_released_event(...)` returned `False`.
- Passing checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py::test_destroy_compose_file_only_records_runtime_released_after_cleanup -q`
  passed.
- Passing adjacent behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py::test_destroy_partial_cleanup_records_runtime_released_when_compose_down_succeeded tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py::test_destroy_compose_file_only_records_runtime_released_after_cleanup -q`
  passed.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
