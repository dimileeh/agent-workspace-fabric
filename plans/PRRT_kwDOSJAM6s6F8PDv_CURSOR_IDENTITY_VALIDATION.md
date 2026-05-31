# Cursor Runtime Identity Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F8PDv_CURSOR_IDENTITY_PLAN.md`

## Requirement Status

- Preserve explicit Cursor model overrides: Complete.
  Cursor adapter model selection still delegates explicit models directly, and
  existing adapter effort-mapping coverage passes.
- Preserve default/high Cursor behavior: Complete.
  Cursor defaults still use `sonnet-4-thinking` through the shared
  `CURSOR_DEFAULT_THINKING_MODEL` constant.
- Stop reporting `sonnet-4-thinking` for lower Cursor efforts without an
  explicit model: Complete.
  Workspace observability and PR identity now use the same Cursor runtime model
  selection as the adapter.
- Keep selected launch preflight ready when Cursor auth and `cursor-agent` are
  available without an AWF-selected model: Complete.
  Selected preflight still probes `cursor-agent` for Cursor even when the model
  value is `None`, and launch admission does not require a selected model for
  that Cursor implicit-default case.
- Keep changes scoped to model-selection metadata and focused tests: Complete.

## Evidence

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_effective_agent_identity_cursor_lower_effort_uses_implicit_runtime_model tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_selected_cursor_preflight_lower_effort_uses_implicit_runtime_model tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_agent_pr_identity_cursor_lower_effort_omits_thinking_model -q`
  failed with all three tests still seeing `sonnet-4-thinking`.
- Regression rerun:
  same command passed: `3 passed`.
- Adapter slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_lower_effort_without_model_override_omits_thinking_model tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
  passed: `2 passed`.
- Executor helper slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py -q -k "agent_model_for_workspace or agent_pr_identity or agent_defaults_for_workspace"`
  passed: `5 passed, 27 deselected`.
- Provider readiness Cursor slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "selected_cursor_preflight or lower_effort_uses_implicit_runtime_model or preflight_reason_and_message_report_missing_model"`
  passed: `5 passed, 45 deselected`.
- Workspace identity slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q -k "effective_agent_identity"`
  passed: `13 passed, 47 deselected`.
- Provider preflight helper slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "preflight_payload or preflight_reason_and_message_report_missing_model"`
  passed: `3 passed, 47 deselected`.
- Changed-file lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/adapters/model_selection.py src/awf/adapters/defaults.py src/awf/adapters/cursor.py src/awf/service/workspace_observability.py src/awf/service/provider_readiness.py src/awf/control/executor/helpers.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py`
  passed.
- Changed-source type check:
  `uv run --python 3.12 --extra dev mypy src/awf/adapters/model_selection.py src/awf/adapters/defaults.py src/awf/adapters/cursor.py src/awf/service/workspace_observability.py src/awf/service/provider_readiness.py src/awf/control/executor/helpers.py`
  passed.

Full AWF/GitHub validation was intentionally not run inside this agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.
