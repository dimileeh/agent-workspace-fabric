# Cursor Effort Default Model Plan

## Context

Review thread `PRRT_kwDOSJAM6s6F8FQ9` reports that Cursor workspaces with a
lower `agent_effort` and no explicit model still run with
`-m sonnet-4-thinking`. The adapter receives the default model before effort
mapping, so the non-high effort path in `_cursor_model_for_effort()` is never
reached.

## Scope

- Preserve explicit Cursor model overrides exactly, including
  `sonnet-4-thinking`.
- Preserve the default high/xhigh Cursor behavior that selects
  `sonnet-4-thinking`.
- Let lower Cursor efforts without an explicit model omit `-m` so Cursor uses
  its normal default.
- Keep changes limited to agent model selection plumbing and focused tests.

## Steps

1. Add failing regression coverage for Cursor lower effort without an explicit
   model at the adapter level.
2. Add failing regression coverage for the executor path so workspace
   `task_policy.agent_effort` without `task_policy.agent_model` does not pass
   `sonnet-4-thinking` as an explicit run model.
3. Adjust adapter run plumbing so `_cli_args()` receives the explicitly
   requested run model, while non-Cursor adapters continue to apply their bound
   defaults internally.
4. Teach the Cursor adapter to treat its thinking default as effort-derived
   unless the run model was explicitly supplied.
5. Run focused adapter/executor tests only and record results in validation.

## Validation

Targeted commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k "CursorAdapter or all_adapters_keep_oversized_prompts_out_of_argv or adapter_cli_args_contract_excludes_prompt_payload or CentralDefaults or Registry"`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_002.py -q -k "cursor_lower_effort_without_model or task_policy_agent_model_overrides_adapter_default or pr_monitor_receives_adapter_bound_to_workspace_model"`

Full AWF/GitHub validation remains managed by AWF after this agent phase.
