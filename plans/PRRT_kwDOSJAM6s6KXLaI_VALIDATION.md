# PRRT_kwDOSJAM6s6KXLaI pre-push dirty finalize operation-owned scoping validation

## Result
Implemented the operation-owned scoping fix from
`plans/PRRT_kwDOSJAM6s6KXLaI_PLAN.md`. The pre-push dirty finalize is now
gated on the dirty paths being a subset of the current monitor operation's
committed delta (`operation_start_head..HEAD`), not merely on the presence
of monitor state.

## Root-cause coverage
- Review thread `PRRT_kwDOSJAM6s6KXLaI`: unrelated files introduced after the
  repair-start dirty guard (e.g. by a failed cleanup or another local
  process) no longer bypass `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. The
  finalize skips when any dirty path is outside the operation's committed
  delta, when no operation-owned anchor is available, or when the delta
  cannot be resolved — preserving the fail-closed path the dirty-recovery
  plan promised ("Keep all existing fail-closed behavior for unrelated
  pre-existing dirt").
- The 3 repair callers that already compute `operation_start_head`
  (`_run_ci_fix`, `_run_fix_cycle`, `_run_operator_hint_repair`) now thread
  it into pre-push validation so the finalize can scope on operation-owned
  paths.
- `_run_sync_base` has no natural operation-start anchor and would require
  capturing a pre-merge HEAD (an extra `git rev-parse` that shifts the
  queued-result sequence of ~16 sync_base tests). It passes no anchor, so
  the finalize stays fail-closed there — the conservative, safe behavior
  the reviewer asked for, and no existing test asserted sync_base finalized
  dirty state, so no behavior regression.

## Verification (focused only — broad validation owned by AWF/GitHub)
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Passed: `34 passed` (3 new finalize-scoping regression tests added:
    unrelated-dirt fail-closed, no-anchor fail-closed, delta-unavailable
    fail-closed; 6 existing finalize tests updated to thread
    `operation_start_head` + queue the operation-delta diff result).
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q`
  - Passed: `60 passed` (no regression in the fix-pass / cleanup / repairs
    surfaces that route through the updated API with the default
    `operation_start_head=None`).
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_parts/ tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ -q`
  - Passed: `434 passed` (no regression in the repair callers or sync_base
    after threading `operation_start_head` from the 3 repair callers and
    leaving sync_base on the default `None` anchor).
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  - Passed
- `uv run --python 3.12 --extra dev ruff format --check <same files>`
  - Passed (6 files already formatted)
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/remote_ops.py`
  - Passed (no issues in 5 source files)

## Notes
- Full AWF/GitHub broad validation (full coverage gate, whole-repo suites,
  frontend builds) is managed by AWF after agent completion per the
  workspace contract; only the focused checks above were run in-agent.
- The fully-correct operation-owned tracking (recording the *specific* paths
  the agent produced, not just paths in the committed delta) remains a
  future design follow-up; this fix closes the over-commit gap for the
  primary scenario (unrelated dirt on untouched paths) and for the
  anchorless/delta-unavailable cases, matching the dirty-recovery plan's
  stated safety contract.
