# PR349 Lint Format CI Fix Plan

## Problem Statement And Scope

PR #349 has a failing GitHub Actions `lint-and-type` check. The job log shows
`ruff check .` passed, then `ruff format --check .` failed because
`tests/unit/runtime/test_pr_monitor_remote_ops.py` would be reformatted.

Scope is limited to restoring formatter compliance for the affected file and
recording focused evidence. Broad AWF/GitHub-owned validation remains managed by
AWF after this agent phase.

## Requirements Checklist

- Preserve the current AWF-managed git branch; do not switch, push, rebase, or
  force-push.
- Do not edit protected workflow, quality-gate, or configuration files.
- Make the smallest change that addresses the observed `lint-and-type` failure.
- Run focused local verification for the changed formatting surface only.
- Recheck PR #349 status after the local fix to discover any newly completed
  failing checks, without running broad local validation.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Format only `tests/unit/runtime/test_pr_monitor_remote_ops.py`.
2. Inspect the resulting diff to confirm it is formatting-only.
3. Run `ruff format --check` for the affected file.
4. Run `ruff check` for the affected file if available through the local
   project tooling.
5. Recheck PR #349 checks via `gh pr checks 349`.
6. Write validation evidence in `plans/PR349_LINT_FORMAT_CI_FIX_VALIDATION.md`.
7. Commit the plan, validation, and formatting fix locally.

## Verification Commands And Pass Criteria

- `.venv/bin/ruff format --check tests/unit/runtime/test_pr_monitor_remote_ops.py`
  or `uv run --python 3.12 --extra dev ruff format --check
  tests/unit/runtime/test_pr_monitor_remote_ops.py` passes.
- `.venv/bin/ruff check tests/unit/runtime/test_pr_monitor_remote_ops.py` or
  `uv run --python 3.12 --extra dev ruff check
  tests/unit/runtime/test_pr_monitor_remote_ops.py` passes.
- `gh pr checks 349 --json name,state,bucket,link,startedAt,completedAt,workflow`
  is used only to observe remote check state after the local fix.
