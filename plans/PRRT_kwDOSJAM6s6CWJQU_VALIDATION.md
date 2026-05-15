# PRRT_kwDOSJAM6s6CWJQU Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CWJQU_PLAN.md`

## Requirement Status

- Complete: Added a regression where the monitor adapter default is the explicit
  failed model while provider recovery receives the configured Codex default.
- Complete: Preserved monitor execution binding by leaving
  `_agent_defaults_for_workspace()` behavior unchanged.
- Complete: Passed the raw configured default from executor monitor handoffs via
  `_call_pr_monitor_factory()`.
- Complete: Kept legacy one-, two-, and three-argument monitor factories
  compatible by probing the new keyword-aware call first and falling back to the
  old signatures.
- Complete: Validated targeted unit tests plus lint and source type checking.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `src/awf/runtime/release_pr_monitor.py`
- `src/awf/control/executor.py`
- `src/awf/service/worker.py`
- `tests/unit/runtime/_monitor_runner_fixtures.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/control/test_executor.py`
- `plans/PRRT_kwDOSJAM6s6CWJQU_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CWJQU_VALIDATION.md`

Commands run:

- Red before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q`
- Red before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_call_pr_monitor_factory_passes_provider_recovery_default_when_supported -q`
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q`
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_call_pr_monitor_factory_passes_provider_recovery_default_when_supported tests/unit/control/test_executor_coverage_edges.py::test_call_pr_monitor_factory_uses_widest_supported_signature tests/unit/control/test_executor_error_paths.py::TestPrMonitorFactoryPath::test_monitor_factory_supports_one_two_and_three_argument_forms -q`
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q`
- Green:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py::TestHappyPath::test_pr_monitor_receives_adapter_bound_to_workspace_model -q`
- Green:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/runtime/release_pr_monitor.py src/awf/control/executor.py src/awf/service/worker.py tests/unit/runtime/_monitor_runner_fixtures.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_executor.py`
- Green:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/control/executor.py tests/unit/control/test_executor.py`
- Green:
  `uv run --python 3.12 --extra dev mypy src/awf`

## Gaps

None.
