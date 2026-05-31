# Review PRRT_kwDOSJAM6s6F7DSu Quoted Porcelain Paths Plan

## Problem Statement And Scope

The PR monitor autofix retry path compares paths parsed from `git status --porcelain`
with paths reported by deterministic pre-commit fixers. Git C-quotes porcelain
paths that contain spaces, while pre-commit fixer output reports the unquoted
repository path. This can make a valid autofix retry look unsafe and leave the
repair dirty.

Scope is limited to parsing quoted porcelain paths used by the PR monitor
autofix retry and focused regression coverage for the reported case.

## Requirements Checklist

- Add a regression test showing a deterministic autofix retry restages a path
  whose porcelain status path is C-quoted because it contains spaces.
- Normalize quoted porcelain paths before safety set comparisons and before
  passing restage paths to `git add`.
- Preserve existing unsafe-path checks and bounded restaging behavior.
- Run only targeted validation for the changed runtime tests, with broad
  AWF/GitHub validation left to AWF after agent completion.

## Implementation Steps

1. Add the focused regression test to `tests/unit/runtime/test_pr_monitor_commit_autofix.py`.
2. Confirm the new test fails against the current implementation when practical.
3. Add porcelain path unquoting in the status parsing path used by the retry.
4. Run the focused test file, plus narrow lint/type checks if needed for touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q`
  must pass.
- If syntax or lint risk warrants it, run a focused `ruff check` on the changed
  source and test files.
- Do not run full repository tests, full coverage, frontend builds, or
  CI-equivalent validation in the agent phase.
