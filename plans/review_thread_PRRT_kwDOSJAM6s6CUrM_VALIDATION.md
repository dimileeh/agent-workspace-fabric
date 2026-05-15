# Review Thread PRRT_kwDOSJAM6s6CUrM Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CUrM_PLAN.md`

## Requirement Status

- Prove the current executor path fails for an explicit non-default Codex model
  capacity failure when no explicit fallback policy is configured: Complete.
- Pass the raw configured Codex default into provider recovery from the executor
  instead of the workspace-overridden adapter default: Complete.
- Preserve normal adapter execution defaults so an explicit workspace model is
  still used for the initial agent run: Complete.
- Preserve PR monitor adapter binding behavior covered by existing monitor
  tests: Complete.
- Run the focused regression and a targeted lint check: Complete.

## Evidence

Files changed:

- `src/awf/control/executor.py`
- `tests/unit/control/test_executor_error_paths.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6CUrM_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CUrM_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestUnexpectedErrorDuringAgentRun::test_provider_recovery_explicit_codex_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_monitor_explicit_model_capacity_falls_back_to_configured_default -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_error_paths.py tests/unit/runtime/test_pr_monitor_runner.py
```

Results:

- The updated executor regression failed before implementation because the
  retry workspace kept `gpt-5.3-codex-spark` and selected
  `PROVIDER_RETRY_DELAYED`.
- After implementation, the executor regression passed and selected fallback to
  the configured Codex default `gpt-5`.
- The monitor explicit-model fallback regression passed.
- Ruff passed.
