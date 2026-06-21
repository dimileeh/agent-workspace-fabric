# PRRT_kwDOSJAM6s6KXLaI preserve commit-sink failures when recording provider outages

## Problem statement
The second review comment on thread `PRRT_kwDOSJAM6s6KXLaI` (discussion
r3431603601) points at `src/awf/runtime/pr_monitor_runner/ci_ops.py:122`.
In `_run_ci_fix`, the three commit-sink `except` handlers
(`ProtectedScopeDiffError`, `_MonitorAgentRuntimeOwnershipRepairFailedError`,
`_MonitorPolicyBlockedError`) each do:

```python
if agent_run_err is not None:
    await self._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
return <commit-sink failure _GitPushResult>
```

`_handle_provider_agent_run_error`
(`src/awf/runtime/pr_monitor_runner/provider_ops.py:288`) records the
provider recovery state and then, for recoverable errors, **raises**:
`ProviderRecoveryRetryError` (line 301), `ProviderRecoveryFallbackError`
(line 299), or `ProviderRecoveryAuthError` (line 303). The
`return <commit-sink failure result>` below the `await` is therefore
unreachable whenever the provider error is recoverable.

Consequences (verified against the code):
1. The commit-sink failure result (protected-scope diff unavailable /
   ownership repair failed / policy blocked) is never returned to the
   loop. The loop's `except ProviderRecoveryRetryError` handler surfaces
   `PROVIDER_OUTAGE` instead of the specific commit-sink reason code, so
   the operator never sees the real cause.
2. When `_commit_dirty_worktree` raised *before* committing (e.g. the
   protected-scope diff could not be verified), the worktree is left
   dirty. The loop records a provider retry and re-raises. On the next
   attempt `_pre_existing_dirty_repair_worktree_result` sees the dirty
   tree and fail-closes with `PRE_EXISTING_DIRTY_WORKTREE`, masking the
   specific commit-sink failure — exactly the scenario the reviewer
   describes.

The existing regression test
`test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
(part_005:662) uses a spy that returns `"deterministic"` and never
raises, so it does NOT cover the recoverable case the reviewer points at.

## Fix
Record the provider recovery state (the side effect we need) WITHOUT
letting the recovery control-flow exception clobber the commit-sink
failure result. Concretely: in the three commit-sink `except` handlers
in `ci_ops.py`, call `_record_provider_agent_run_error` (the recording
helper) directly — or catch the recovery control-flow exceptions around
the `_handle_provider_agent_run_error` call — so the commit-sink
failure result is still returned.

The minimal, scoped change is to wrap the
`_handle_provider_agent_run_error` call in each of the three handlers
with a `try/except` that swallows
`ProviderRecoveryRetryError`/`ProviderRecoveryFallbackError`/
`ProviderRecoveryAuthError`. `_persist_state` already ran inside
`_handle_provider_agent_run_error` before the raise, and
`_record_provider_agent_run_error` already persisted the provider
recovery attempt row, so the recovery state is recorded; only the
control-flow raise is suppressed so the commit-sink result wins.

This mirrors the existing pattern at `comments.py:285-290` and
`remote_ops.py:754-755` where `_handle_provider_agent_run_error` is
awaited on the success path (no commit-sink failure to preserve). Here
a commit-sink failure exists and must win.

## Scope
- Only `src/awf/runtime/pr_monitor_runner/ci_ops.py` lines 120-150 (the
  three commit-sink `except` handlers). No other file changes.
- Add a regression test in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
  that asserts: when the adapter raises `AgentRunError` AND
  `_commit_dirty_worktree` raises one of the three commit-sink
  exceptions AND `_handle_provider_agent_run_error` would raise
  `ProviderRecoveryRetryError`, `_run_ci_fix` returns the commit-sink
  failure `_GitPushResult` (not propagate the retry).
- No refactor, no new abstractions, no protected-file edits.

## Requirements checklist
- [ ] Add a regression test (TDD red) covering the recoverable provider
      error + commit-sink failure case for all three commit-sink
      exceptions, asserting the commit-sink failure result is returned
      and `ProviderRecoveryRetryError` is NOT propagated.
- [ ] Confirm the new test fails on the current code (TDD red).
- [ ] Implement the minimal fix in `ci_ops.py` (swallow the recovery
      control-flow exceptions in the three commit-sink handlers).
- [ ] Confirm the new test passes (TDD green).
- [ ] Confirm the existing
      `test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
      still passes (the deterministic/non-raising handler path is
      unchanged).
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps
1. Write the regression test parametrized over the three commit-sink
   exceptions, with `_handle_provider_agent_run_error` raising
   `ProviderRecoveryRetryError`, asserting `_run_ci_fix` returns the
   commit-sink failure result.
2. Run it, confirm it fails (TDD red — currently the retry propagates).
3. In `ci_ops.py`, wrap each of the three
   `_handle_provider_agent_run_error` calls in the commit-sink handlers
   with `try/except (ProviderRecoveryRetryError,
   ProviderRecoveryFallbackError, ProviderRecoveryAuthError): pass`.
4. Re-run the new test (TDD green) and the existing regression test.
5. Lint/typecheck the touched files.

## Verification commands (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py`

## Pass criteria
- New regression test fails on unfixed code, passes on fixed code.
- Existing `test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
  still passes (deterministic handler path unchanged).
- Lint/typecheck clean on touched files.
