# Review Thread PRRT_kwDOSJAM6s6CT5YH Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/control/executor.py`.
The review reports that executor provider-recovery preparation persists the
adapter default model but ignores workspace task-policy model overrides, even
though execution binds those overrides into the adapter defaults.

Scope is limited to provider-recovery preparation and focused regression
coverage.

## Requirements Checklist

- Add a regression test showing a Codex workspace `agent_model` override is
  treated as the effective default when scheduling provider recovery.
- Update `_prepare_provider_recovery` to resolve defaults through
  `_agent_defaults_for_workspace`.
- Preserve existing behavior when workspace lookup fails or the agent runtime
  has no configured defaults.
- Run the narrow focused regression and relevant lint/type checks.

## Implementation Steps

1. Add a focused executor regression covering a Codex capacity failure with a
   workspace-level model override and no explicit fallback policy.
2. Confirm the regression fails before implementation.
3. Update `src/awf/control/executor.py` so the persisted
   `effective_default_model` uses workspace-bound defaults.
4. Re-run the focused regression and targeted validation commands.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestUnexpectedErrorDuringAgentRun::test_provider_recovery_uses_workspace_model_override_as_effective_default -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_error_paths.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor.py
```

Pass criteria: the new regression fails before the implementation change, then
passes along with targeted lint and type checks.
