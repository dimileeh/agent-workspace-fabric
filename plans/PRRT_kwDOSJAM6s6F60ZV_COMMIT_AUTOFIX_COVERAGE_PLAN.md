# PRRT_kwDOSJAM6s6F60ZV Commit Autofix Coverage Plan

## Problem Statement And Scope

The review thread reports missing unit coverage for defensive early-return paths in
`_retry_monitor_precommit_autofix_commit_once`. The scope is limited to focused
regression tests for those branches; no source behavior change is planned unless
the tests expose a defect.

## Requirements Checklist

- Add focused unit coverage for the failed `git status --porcelain` branch.
- Add focused unit coverage for the clean-worktree early exit branch.
- Add focused unit coverage for the failed `git add -- <restage_paths>` branch.
- Keep the fix limited to the PR monitor commit autofix test surface.
- Run only targeted local validation; full AWF/GitHub validation remains owned
  by AWF after this agent phase.

## Implementation Steps

1. Inspect existing commit autofix tests and test helpers.
2. Add regression tests in `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
   for the three reported branches.
3. Run the narrow unit test file.
4. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  must pass.
- Do not run full coverage, whole-repository unit suites, or CI-equivalent
  validation in this workspace phase.
