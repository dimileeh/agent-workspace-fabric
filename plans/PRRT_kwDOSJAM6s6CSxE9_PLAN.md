# PRRT_kwDOSJAM6s6CSxE9 Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CSxE9` reports that Codex provider recovery
uses `DEFAULT_AGENT_DEFAULTS` when deciding whether a capacity failure should
fall back to the default model. That is wrong when a deployment overrides the
Codex default through `ExecutorConfig.default_models` or `agent_defaults` and a
workspace inherits that configured default without a `task_policy.agent_model`.

Scope is limited to provider-recovery default-model selection and the callers
that know the workspace's effective configured default. No branch changes or
pushes.

## Requirements Checklist

- Add a regression proving a Codex capacity failure on an operator-configured
  default model follows the retry/cooldown path instead of falling back to the
  built-in Codex default.
- Keep existing explicit non-default Codex fallback behavior intact when the
  workspace policy declares `agent_model`.
- Pass effective default model context from executor and PR monitor recovery
  callers where available.
- Skip implicit built-in fallback when no explicit policy model or effective
  default model is available.
- Preserve provider recovery reason codes, state payloads, and existing
  explicit fallback policy behavior.

## Implementation Steps

1. Add a focused failing regression in provider recovery tests.
2. Thread an optional effective default model through provider recovery
   decision and attempt creation.
3. Update executor and PR monitor callers to provide the configured/adapter
   default model where they know it.
4. Run focused tests, then run formatter/lint checks appropriate to touched
   Python files.
5. Write validation evidence in `plans/PRRT_kwDOSJAM6s6CSxE9_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/provider_recovery.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py src/awf/adapters/base.py tests/unit/service/test_provider_recovery.py tests/unit/runtime/test_pr_monitor_runner.py`

Pass criteria: the new regression fails before implementation, all listed
commands pass after implementation, and no unrelated files are staged.
