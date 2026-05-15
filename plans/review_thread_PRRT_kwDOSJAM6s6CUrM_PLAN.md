# Review Thread PRRT_kwDOSJAM6s6CUrM Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/control/executor.py`.
The review reports that executor provider recovery passes a workspace-overridden
Codex model as the recovery "default", causing capacity recovery for an
explicit non-default Codex model to retry the exhausted model instead of
falling back to the deployment's configured Codex default.

Scope is limited to the executor recovery default passed into provider
recovery and the focused executor regression that covers this path.

## Requirements Checklist

- Prove the current executor path fails for an explicit non-default Codex model
  capacity failure when no explicit fallback policy is configured.
- Pass the raw configured Codex default into provider recovery from the executor
  instead of the workspace-overridden adapter default.
- Preserve normal adapter execution defaults so an explicit workspace model is
  still used for the initial agent run.
- Preserve PR monitor adapter binding behavior covered by existing monitor
  tests.
- Run the focused regression and a targeted lint check.

## Implementation Steps

1. Update the focused executor provider-recovery regression so it expects a
   fallback to the configured Codex default model.
2. Run that regression and confirm it fails before the implementation change.
3. Change `_prepare_provider_recovery` to pass `defaults.model` from
   `_defaults_for(...)` directly to `create_provider_recovery_attempt_row`.
4. Re-run the focused regression and targeted checks.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestUnexpectedErrorDuringAgentRun::test_provider_recovery_explicit_codex_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_error_paths.py tests/unit/runtime/test_pr_monitor_runner.py
```

Pass criteria: the focused executor regression fails before implementation,
then passes after the executor uses the configured default; the monitor
regression remains green; ruff reports no issues.
