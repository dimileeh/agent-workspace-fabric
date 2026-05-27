# Fix Readyz Retained Worktree CI Plan

## Problem Statement and Scope

PR #288 CI reports a failure in
`tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy`
at the readiness orphan-resource assertion. The AWF-provided focused repro passes
when run alone, so the likely scope is test-order state isolation or a readiness
orphan scan edge case exposed only by neighboring tests.

Scope is limited to the failing `/readyz` health behavior and its focused unit
tests. Do not weaken CI checks, skip tests, edit workflow gates, switch
branches, push, or run full coverage locally.

## Requirements Checklist

- Reproduce or narrow the CI failure with focused pytest commands.
- Identify the root cause behind the terminal retained-worktree readiness result.
- Add or adjust a regression test when behavior changes.
- Implement the smallest code/test fix consistent with existing `src/awf`
  patterns.
- Run focused verification only; leave full AWF/GitHub validation to AWF after
  agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Run the AWF-provided single-test repro and record whether it fails locally.
2. Inspect the relevant `/readyz` test fixture, the failing test, and the orphan
   resource scanning code.
3. Run focused neighboring test subsets to find an order-dependent repro.
4. Patch the isolated root cause, preferring fixture cleanup or deterministic
   health state over broad behavior changes.
5. Run the narrowed repro plus the AWF-provided focused test.
6. Write `plans/fix_readyz_retained_worktree_ci_VALIDATION.md` with
   requirement-by-requirement evidence.
7. Commit the local changes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy -q`
  - Passes.
- Additional focused pytest command for the narrowed neighboring/order-dependent
  repro.
  - Passes and covers the CI root cause.

Full repository tests, full coverage, frontend builds, and CI-equivalent gates
are intentionally not run locally; AWF/GitHub own those broad validation steps.
