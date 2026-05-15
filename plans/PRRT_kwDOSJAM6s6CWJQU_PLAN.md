# PRRT_kwDOSJAM6s6CWJQU Plan

## Problem Statement and Scope

The PR monitor records provider recovery attempts using `adapter.default_model` as
the effective default model. In production handoff paths the executor binds a
workspace explicit `task_policy.agent_model` into the adapter, so this value can
be the failed explicit model instead of the raw configured Codex default. That
suppresses the intended capacity fallback from an explicit Codex model to the
configured Codex default.

Scope is limited to PR monitor provider-recovery default selection and the
executor-to-monitor handoff that supplies the raw default.

## Requirements Checklist

- Add a regression test where the monitor adapter default is the explicit failed
  model but recovery still falls back to the configured Codex default.
- Preserve the existing behavior that monitor agent runs use the workspace-bound
  adapter model.
- Pass the raw configured agent default into monitor construction from executor
  factory handoffs where available.
- Keep legacy one-, two-, and three-argument PR monitor factories compatible.
- Validate the targeted unit tests.

## Implementation Steps

1. Extend `PullRequestMonitorRunner` construction/dependencies with an optional
   provider-recovery default model distinct from `adapter.default_model`.
2. Use that explicit recovery default when recording provider agent run errors,
   falling back to `adapter.default_model` only for legacy direct construction.
3. Extend feature/release monitor builders to accept and forward the recovery
   default model.
4. Extend executor monitor factory calls to pass the raw default returned by
   `_defaults_for(agent)` while keeping the adapter default bound through
   `_agent_defaults_for_workspace()`.
5. Update tests and fixtures for the production-path regression and factory
   compatibility.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_call_pr_monitor_factory_passes_provider_recovery_default_when_supported tests/unit/control/test_executor_coverage_edges.py::test_call_pr_monitor_factory_uses_widest_supported_signature tests/unit/control/test_executor_error_paths.py::TestPrMonitorFactoryPath::test_monitor_factory_supports_one_two_and_three_argument_forms -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q`
