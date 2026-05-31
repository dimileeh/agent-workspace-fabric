# Cursor Effort Default Model Validation

## Plan Conformance

- Add failing adapter regression: Complete.
  `TestCursorAdapter.test_lower_effort_without_model_override_omits_thinking_model`
  failed before implementation because the Cursor CLI tail still contained
  `-m sonnet-4-thinking`.
- Add failing executor regression: Complete.
  `TestHappyPathPart001.test_cursor_lower_effort_without_model_override_omits_thinking_model`
  failed before implementation for the same `-m sonnet-4-thinking` tail in the
  workspace execution path.
- Preserve explicit model overrides: Complete.
  `test_explicit_thinking_model_override_is_preserved_for_lower_effort` proves
  an explicit `model="sonnet-4-thinking"` still emits `-m sonnet-4-thinking`.
- Preserve non-Cursor default behavior: Complete.
  Non-Cursor adapters now apply their bound defaults inside `_cli_args()`, while
  `AgentAdapter.run()` passes only the explicit per-run model override.
- Apply Cursor effort mapping for lower efforts: Complete.
  Cursor now treats the bound `sonnet-4-thinking` default as effort-derived, so
  lower efforts without an explicit model omit `-m`.

## Focused Validation

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_lower_effort_without_model_override_omits_thinking_model tests/unit/control/test_executor_parts/test_executor_part_002.py::TestHappyPathPart001::test_cursor_lower_effort_without_model_override_omits_thinking_model -q`
  failed with both tests still seeing `-m sonnet-4-thinking`.
- Regression rerun:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_lower_effort_without_model_override_omits_thinking_model tests/unit/control/test_executor_parts/test_executor_part_002.py::TestHappyPathPart001::test_cursor_lower_effort_without_model_override_omits_thinking_model -q`
  passed: `2 passed`.
- Adapter slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k "CursorAdapter or all_adapters_keep_oversized_prompts_out_of_argv or adapter_cli_args_contract_excludes_prompt_payload or CentralDefaults or Registry"`
  passed: `16 passed, 34 deselected`.
- Executor slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_002.py -q -k "cursor_lower_effort_without_model or task_policy_agent_model_overrides_adapter_default or pr_monitor_receives_adapter_bound_to_workspace_model"`
  passed: `3 passed, 18 deselected`.
- Changed-file lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/adapters/base.py src/awf/adapters/claude_code.py src/awf/adapters/codex.py src/awf/adapters/cursor.py src/awf/adapters/gemini.py src/awf/adapters/opencode.py src/awf/control/executor/helpers.py src/awf/control/executor/execution_flow.py src/awf/control/executor/execution_validation.py tests/unit/adapters/test_adapters.py tests/unit/control/test_executor_parts/test_executor_part_002.py`
  passed.
- Changed-source type check:
  `uv run --python 3.12 --extra dev mypy src/awf/adapters/base.py src/awf/adapters/claude_code.py src/awf/adapters/codex.py src/awf/adapters/cursor.py src/awf/adapters/gemini.py src/awf/adapters/opencode.py src/awf/control/executor/helpers.py src/awf/control/executor/execution_flow.py src/awf/control/executor/execution_validation.py`
  passed.
- Adapter file:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q`
  passed: `50 passed`.

Full AWF/GitHub validation is intentionally not run inside this agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.
