# REVIEW 4457797228 History-Rewritten Plan

## Problem Statement and Scope

Address the remaining actionable part of review comment `issue:4457797228`:
the protected-scope committed-repair path logs
`monitor.protected_scope_committed_repair_commit_not_created` for both a clean
history rewrite and a no-op repair. Operators need a boolean on that event so
the successful history-rewrite case is distinguishable from an agent run that
left `HEAD` unchanged.

The provider recovery default-model concern is already handled in this branch:
when no effective default model is supplied,
`test_codex_capacity_without_effective_default_skips_implicit_fallback` proves
AWF does not silently fall back to the compiled-in default.

## Requirements Checklist

- Add regression coverage that a clean no-commit protected-scope repair records
  `history_rewritten=True` when the agent changes local `HEAD`.
- Preserve the existing clean no-commit behavior and successful push flow.
- Keep provider recovery behavior unchanged and verify the existing stale-review
  coverage still passes.
- Run focused tests and lint for the touched files.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Update the protected-scope committed-repair log regression to simulate a
   different `HEAD` before and after the monitor agent run and assert the
   captured warning includes `history_rewritten=True`.
2. Run the targeted test to confirm it fails before the runtime change.
3. Capture local `HEAD` before and after the agent run in
   `_repair_protected_scope_commits_before_push`.
4. Include `history_rewritten` on the clean no-commit warning.
5. Run the targeted runtime test, provider recovery fallback guard test, and
   ruff for the touched files.
6. Write `plans/REVIEW_4457797228_HISTORY_REWRITTEN_VALIDATION.md`.
7. Stage only changed files and commit with a conventional message for comment
   `4457797228`.

## Verification Commands and Pass Criteria

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  must fail after the regression is tightened.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_commit_repair_logs_when_dirty_commit_not_created -q`
  must pass.
- Provider recovery stale-review coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py::test_codex_capacity_without_effective_default_skips_implicit_fallback -q`
  must pass.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/service/test_provider_recovery.py`
  must pass.
