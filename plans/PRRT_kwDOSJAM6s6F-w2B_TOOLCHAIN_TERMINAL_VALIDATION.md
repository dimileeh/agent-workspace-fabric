# PRRT_kwDOSJAM6s6F-w2B Toolchain-Missing Terminal Failure Validation

## Result

The review thread is fixed. A failed `_GitPushResult` with reason
`PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` now reports
`terminal_monitor_failure is True`, so monitor handlers fail fast with the
specific pre-push validation reason instead of retrying until the decision-loop
limit.

## Plan Validation

| Plan item | Status | Evidence |
| --- | --- | --- |
| Add a failing regression for toolchain-missing terminal classification. | Complete | `tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py` failed before the production change with `False is True`. |
| Include `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` in terminal monitor failures. | Complete | `src/awf/runtime/pr_monitor_runner/remote_ops.py` now includes `_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON` in `_GitPushResult.terminal_monitor_failure`. |
| Keep the change scoped to PR monitor push result classification. | Complete | Production diff is limited to `remote_ops.py`; test diff is a focused companion regression file. |

## Focused Checks

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py -q
```

Result: `9 passed in 0.72s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py
```

Result: `All checks passed!`.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, and merge gating after completion.
