# Cursor Runtime Identity Plan

## Context

Review thread `PRRT_kwDOSJAM6s6F8PDv` reports that Cursor workspaces with a
lower `agent_effort` and no explicit `agent_model` run without `-m`, but launch
preflight and PR identity still report AWF's high-effort
`sonnet-4-thinking` default. That makes operator-facing metadata disagree with
the actual Cursor CLI invocation.

## Scope

- Preserve explicit Cursor model overrides exactly.
- Preserve the default/high Cursor behavior that reports and selects
  `sonnet-4-thinking`.
- For lower Cursor efforts without an explicit model, stop reporting
  `sonnet-4-thinking`; metadata should reflect that AWF lets Cursor use its CLI
  implicit default.
- Keep selected launch preflight ready when Cursor auth and `cursor-agent` are
  available, even when AWF does not pass `-m`.
- Keep changes limited to model-selection metadata and focused regression tests.

## Steps

1. Add failing regression coverage for Cursor lower-effort identity in
   workspace observability.
2. Add failing regression coverage for selected Cursor launch preflight with a
   lower effort and no explicit model.
3. Add failing regression coverage for generated PR identity with a lower
   Cursor effort and no explicit model.
4. Share Cursor runtime model selection between the adapter and metadata
   surfaces so all paths agree on whether a model is explicitly selected.
5. Run focused tests for the changed behavior and record results in validation.

## Validation

Targeted commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_effective_agent_identity_cursor_lower_effort_uses_implicit_runtime_model tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_selected_cursor_preflight_lower_effort_uses_implicit_runtime_model tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_agent_pr_identity_cursor_lower_effort_omits_thinking_model -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_lower_effort_without_model_override_omits_thinking_model tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
- Focused changed-file lint/type checks may be run only against touched Python
  files if needed.

Full AWF/GitHub validation remains managed by AWF after this agent phase.
