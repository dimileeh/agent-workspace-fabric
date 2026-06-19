# Validation: PRRT_kwDOSJAM6s6KpAD6 — CI rollback uses stale HEAD

Plan reference: `plans/PRRT_kwDOSJAM6s6KpAD6_CI_FIX_STALE_HEAD_PLAN.md`

## Requirement-by-requirement status

### 1. `post_agent_head` captured inside the provider-recovery `except` block — Complete

The pre-try `post_agent_head = await self._rev_parse_head(worktree_path)` block
was removed from `_run_ci_fix`. The capture now happens INSIDE the
`ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
`ProviderRecoveryAuthError` `except` clause, AFTER `_commit_dirty_worktree`
raised (`src/awf/runtime/pr_monitor_runner/ci_ops.py:289`).

### 2. Captured HEAD passed as `restore_ref`, preserving in-sink self-commits — Complete

`post_agent_head` (captured post-raise) is passed to
`_rollback_ci_fix_residue_before_provider_recovery(..., restore_ref=post_agent_head)`.
`git reset --hard` to this ref preserves any protected-scope repair
self-commit that advanced HEAD inside the sink, discarding only the stranded
residue.

### 3. `None` (HEAD unresolvable post-raise) skips the rollback — Complete

The rollback helper's existing `if restore_ref is None: return` guard
(`ci_ops.py:110`) handles the missing-anchor case. The regression test
`test_ci_fix_commit_sink_provider_recovery_rollback_skipped_when_post_agent_head_unavailable`
now queues a failing `rev-parse HEAD` as the post-raise capture (the pre-try
slot is gone), and asserts no `git reset --hard` runs and the skip is logged.

### 4. Pre-try capture block removed; docstring updated — Complete

The pre-try capture block and its docstring were replaced with a short comment
explaining that the anchor is captured inside the provider-recovery `except`
clause. The `_rollback_ci_fix_residue_before_provider_recovery` docstring was
updated to describe `restore_ref` as the post-raise HEAD (captured inside the
`except` clause), referencing `PRRT_kwDOSJAM6s6KnWkn` (finalize regression)
and `PRRT_kwDOSJAM6s6KpAD6` (this fix).

### 5. Regression test added (test-first) — Complete

`test_ci_fix_commit_sink_provider_recovery_rolls_back_to_post_raise_head_not_pre_sink_head`
(parametrized over the three provider-recovery exception types) was added to
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`.
It simulates the in-sink self-commit via a mutable HEAD cell advanced inside
the mocked `_commit_dirty_worktree` side effect before it raises, then asserts
the rollback `git reset --hard` anchors against the post-raise HEAD
(`in_sink_self_commit_head`), NOT the stale pre-sink HEAD
(`operation_start_head`).

Confirmed the test FAILS against the pre-fix code (rollback anchors against
`abc1234567890def` = stale pre-sink HEAD) and PASSES against the fixed code.

### 6. No other callers or files changed — Complete

Only `src/awf/runtime/pr_monitor_runner/ci_ops.py` was changed for behavior.
Existing regression tests across
`test_pr_monitor_runner_coverage_edges_part_003.py`,
`_part_005.py`, `_part_006.py`, `_part_011.py`, `_part_015.py` had their
`FakeCommandRunner` queue slots updated to match the removed pre-try
`_rev_parse_head` call (the "post-agent HEAD" slot that was consumed by the
pre-try capture is gone; the post-raise capture is now the slot that feeds the
provider-recovery rollback). No assertions were weakened — the existing
regression contracts (preserve agent self-commits, skip on missing anchor,
clean untracked residue, rollback failure does not clobber exception) all
still hold.

## Evidence

Files changed:
- `src/awf/runtime/pr_monitor_runner/ci_ops.py` — moved `post_agent_head`
  capture into the provider-recovery `except` clause; updated docstrings.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`
  — added the new regression test; updated existing CI-fix tests' command
  queues to match the removed pre-try capture.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  — removed the stale "post-agent HEAD" queue slot from two tests.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
  — removed the stale "post-agent HEAD" queue slot from two tests.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
  — removed the stale "post-agent HEAD" queue slot from four tests.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py`
  — removed the stale "post-agent HEAD" queue slot from one test.

Focused verification commands run (agent-phase safe):
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/` — All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py` — Success: no issues found.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_edges.py -q` — 338 passed.

Full AWF/GitHub broad validation (full coverage gate, whole-repo suite) is
managed by AWF after agent completion; the agent did NOT run the broad suite
per workspace contract.

## Gaps

None. All planned requirements are satisfied.
