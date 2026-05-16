# PRRT_kwDOSJAM6s6CSxE9 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CSxE9_PLAN.md`

## Requirement Status

- Add a regression proving a Codex capacity failure on an operator-configured
  default model follows retry/cooldown instead of built-in fallback: Complete.
  Evidence: `test_codex_configured_default_capacity_uses_retry_path` and
  `test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback`.
- Keep existing explicit non-default Codex fallback behavior intact when
  `task_policy.agent_model` is declared: Complete. Evidence: existing
  `test_codex_non_default_capacity_falls_back_to_default_model` still passes.
- Pass effective default model context from executor and PR monitor recovery
  callers where available: Complete. Evidence: `WorkspaceExecutor` passes the
  configured default model into recovery; `PullRequestMonitorRunner` passes the
  adapter default when the workspace has no explicit policy model.
- Skip implicit built-in fallback when no explicit policy model or effective
  default model is available: Complete. Evidence:
  `test_codex_capacity_without_effective_default_skips_implicit_fallback`.
- Preserve provider recovery reason codes, state payloads, and explicit
  fallback policy behavior: Complete. Evidence: full provider recovery and PR
  monitor runner unit files passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_configured_default_capacity_uses_retry_path -q`
  - Initial result before implementation: failed with unexpected
    `effective_default_model` argument.
  - Final result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback -q`
  - Initial result before implementation: failed because recovery state action
    was `fallback` instead of `retry`.
  - Final result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py -q`
  - Final result: 91 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q`
  - Final result: 107 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/provider_recovery.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py src/awf/adapters/base.py tests/unit/service/test_provider_recovery.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py`
  - Final result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Final result: passed.

## Gaps

None.
