# PRRT_kwDOSJAM6s6F8cCn Cursor Recovery Model Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F8cCn_CURSOR_RECOVERY_MODEL_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing lower-effort Cursor recovery handoff
  omits `-m` and passes no provider recovery default model.
- Complete: Preserved non-Cursor monitor handoff behavior for explicit task
  model fallback to the configured runtime default.
- Complete: Updated executor PR monitor factory handoffs to use a shared helper.
- Complete: Avoided broad AWF/GitHub-owned validation; only focused tests,
  focused lint, and targeted mypy were run.
- Complete: Prepared changes for a local conventional commit referencing the
  thread ID.

## Evidence

Files changed:

- `src/awf/adapters/base.py`
- `src/awf/control/executor/helpers.py`
- `src/awf/control/executor/execution_flow.py`
- `src/awf/control/executor/monitor_handoff.py`
- `src/awf/runtime/pr_monitor_runner/provider_ops.py`
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py`
- `plans/PRRT_kwDOSJAM6s6F8cCn_CURSOR_RECOVERY_MODEL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F8cCn_CURSOR_RECOVERY_MODEL_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_recovery_skip_push_cursor_lower_effort_handoff_uses_implicit_runtime_model -q`
  - Failed before implementation because handoff passed `sonnet-4-thinking`.
  - Passed after implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_002.py::TestHappyPathPart001::test_pr_monitor_receives_adapter_bound_to_workspace_model tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_recovery_skip_push_with_factory_resumes_monitor_runner tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
  - Passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/base.py src/awf/control/executor/helpers.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py src/awf/runtime/pr_monitor_runner/provider_ops.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/adapters/base.py src/awf/control/executor/helpers.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py src/awf/runtime/pr_monitor_runner/provider_ops.py`
  - Passed.

Full AWF/GitHub validation was not run in-agent per workspace contract; AWF
owns broad validation after agent completion.
