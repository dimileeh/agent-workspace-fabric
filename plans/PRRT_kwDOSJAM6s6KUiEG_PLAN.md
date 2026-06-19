# PRRT_kwDOSJAM6s6KUiEG CI-fix provider-error early-return plan

## Problem statement
In `_run_ci_fix` (`src/awf/runtime/pr_monitor_runner/ci_ops.py`), the agent
run happens first (lines 94-109), storing an `AgentRunError` in
`agent_run_err`. Then `_commit_dirty_worktree` is invoked (lines 111-119).
If the commit sink raises `ProtectedScopeDiffError`,
`_MonitorAgentRuntimeOwnershipRepairFailedError`, or
`_MonitorPolicyBlockedError`, the function returns early via the
`except` handlers (lines 120-144) — before reaching
`_handle_provider_agent_run_error` (lines 146-147). As a result, when both
an agent run error and a commit-sink failure occur together, the stored
`AgentRunError` is never passed to provider recovery, so retry/cooldown
state is not recorded. That can let a real provider outage escape the
breaker / cooldown bookkeeping on the next cycle.

## Scope
- Single behavior fix in `_run_ci_fix`.
- Pass the stored `agent_run_err` to `_handle_provider_agent_run_error`
  before each of the three early-return commit-sink handlers.
- No new abstractions, no unrelated refactor, no signature changes.

## Requirements checklist
- [ ] Add a regression test that triggers an `AgentRunError` from the
      adapter AND a `_MonitorAgentRuntimeOwnershipRepairFailedError` (and
      `ProtectedScopeDiffError`, and `_MonitorPolicyBlockedError`) from
      `_commit_dirty_worktree`, asserting `_handle_provider_agent_run_error`
      was still invoked / provider recovery state recorded before the early
      return.
- [ ] Confirm the new regression test fails against the current code.
- [ ] Implement the minimal fix: invoke `_handle_provider_agent_run_error`
      on the stored `agent_run_err` (when present) inside each of the three
      early-return handlers before returning.
- [ ] Confirm the regression test passes after the fix.
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the regression test in the existing CI-fix coverage edge file.
2. Run it, confirm it fails (TDD red).
3. Edit `ci_ops.py` to call `_handle_provider_agent_run_error` first in
   each early-return handler. Because `_handle_provider_agent_run_error`
   may raise `ProviderRecoveryFallbackError`/`RetryError`/`AuthError`,
   invoke it before constructing/awaiting the early-return result so its
   provider-recovery side effect and any recovery exception are not lost.
4. Re-run the regression test (TDD green), plus the existing CI-fix tests
   for the three handlers to ensure no regression.
5. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py`

## Pass criteria
- New regression test fails on the unfixed code and passes on the fixed code.
- Existing CI-fix early-return tests still pass (no behavior regression for
  the no-agent-error path).
- Lint/typecheck clean on touched files.
