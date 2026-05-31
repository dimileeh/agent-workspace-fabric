# PRRT_kwDOSJAM6s6F8cCn Cursor Recovery Model Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6F8cCn` reports that PR monitor handoff
passes the central Cursor default model `sonnet-4-thinking` as
`provider_recovery_default_model` even when a workspace lowers Cursor effort and
the Cursor adapter omits `-m`. This can make provider recovery metadata disagree
with the CLI invocation the monitor actually runs.

Scope is limited to PR monitor handoff/provider recovery model selection for
Cursor lower-effort workspaces and narrow regression coverage.

## Requirements Checklist

- Add a regression test showing a lower-effort Cursor recovery handoff omits the
  thinking model both in CLI args and provider recovery default metadata.
- Preserve existing non-Cursor behavior where explicit task models can still
  fall back to the configured runtime default.
- Update all executor PR monitor factory handoffs to use a shared model helper.
- Avoid broad AWF/GitHub-owned validation; run focused tests only.
- Commit the fix locally with a conventional commit referencing the thread ID.

## Implementation Steps

1. Add the failing Cursor lower-effort recovery handoff regression.
2. Add an adapter/provider recovery default model surface that reports the model
   actually selected when no per-run model is passed.
3. Add a helper for monitor handoff default selection: Cursor uses the adapter's
   selected default; other runtimes keep the central configured default.
4. Replace direct `defaults.model` handoff arguments in executor flow and
   monitor handoff code with the helper.
5. Run the focused regression test and adjacent targeted tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_recovery_skip_push_cursor_lower_effort_handoff_uses_implicit_runtime_model -q`
  - Passes after implementation; fails before code fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_002.py::TestHappyPathPart001::test_pr_monitor_receives_adapter_bound_to_workspace_model tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py::test_recovery_skip_push_with_factory_resumes_monitor_runner tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_effort_mapping_uses_documented_models_not_extra_flags -q`
  - Existing nearby behavior remains green.

Full AWF/GitHub validation is intentionally not run in-agent per workspace
contract; AWF owns broad validation after agent completion.
