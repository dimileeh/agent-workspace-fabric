# PRRT_kwDOSJAM6s6CUCZv Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CUCZv_PLAN.md`

## Requirement Status

- Add a regression proving a PR monitor with an explicit Codex `agent_model`
  and a different configured adapter default falls back to the configured
  adapter default, not the built-in default: Complete.
- Keep the existing configured-default retry behavior for workspaces that
  already run on that configured default: Complete.
- Preserve existing provider recovery reason codes, event payloads, and
  in-place monitor mutation behavior: Complete.
- Run focused tests and lint/type checks for touched Python files: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/PRRT_kwDOSJAM6s6CUCZv_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CUCZv_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback tests/unit/service/test_provider_recovery.py::test_codex_non_default_capacity_falls_back_to_default_model tests/unit/service/test_provider_recovery.py::test_codex_configured_default_capacity_uses_retry_path -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q
```

Results:

- New regression failed before implementation because monitor recovery mutated
  `task_policy.agent_model` to built-in `gpt-5.5` instead of the configured
  default `gpt-5.4-mini`.
- Focused regression and adjacent provider recovery tests passed after
  implementation: 4 passed.
- Ruff passed.
- Mypy passed for `src/awf/runtime/pr_monitor_runner.py`.
- Full `tests/unit/runtime/test_pr_monitor_runner.py` passed: 108 passed.

## Gaps

None.
