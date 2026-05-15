# PRRT_kwDOSJAM6s6CUCZv Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CUCZv` reports that PR-monitor provider
recovery passes `None` as `effective_default_model` whenever the workspace has
an explicit `task_policy.agent_model`. For Codex capacity failures this lets
`decide_provider_recovery()` fall back to `DEFAULT_AGENT_DEFAULTS` instead of
the monitor adapter's configured default model.

Scope is limited to PR-monitor provider recovery default-model selection and
focused regression coverage for the reported inline thread. No branch changes
or pushes.

## Requirements Checklist

- Add a regression proving a PR monitor with an explicit Codex `agent_model`
  and a different configured adapter default falls back to the configured
  adapter default, not the built-in default.
- Keep the existing configured-default retry behavior for workspaces that
  already run on that configured default.
- Preserve existing provider recovery reason codes, event payloads, and
  in-place monitor mutation behavior.
- Run focused tests and lint/type checks for touched Python files.

## Implementation Steps

1. Add a focused failing regression in `tests/unit/runtime/test_pr_monitor_runner.py`.
2. Run the focused regression and record the pre-fix failure.
3. Update `PullRequestMonitorRunner._record_provider_agent_run_error()` to
   pass the adapter's configured default model into provider recovery even
   when the workspace policy declares an explicit model.
4. Re-run the focused regression and related provider recovery tests.
5. Write validation evidence in `plans/PRRT_kwDOSJAM6s6CUCZv_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback tests/unit/service/test_provider_recovery.py::test_codex_non_default_capacity_falls_back_to_default_model tests/unit/service/test_provider_recovery.py::test_codex_configured_default_capacity_uses_retry_path -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py
```

Pass criteria: the new regression fails before implementation, passes after the
implementation change, related focused tests pass, and no unrelated files are
staged.
