# PR349 CI Current Fix Plan

## Problem Statement And Scope

PR #349 has a failing GitHub Actions CI run. The last completed failure on the
PR branch is run `26728322700`, where `python-full-coverage` failed because
unit tests failed; the aggregate `ci-required` job failed as a consequence.
The current PR head run `26748691897` is still in progress, so this fix will
focus on the observed unit-test regressions and re-check current CI status
before completion.

## Requirements Checklist

- Keep all work on the current AWF-managed branch; do not switch branches,
  push, rebase, or force-push.
- Do not edit protected workflow or quality-gate configuration files.
- Reproduce the observed failures with targeted unit-test commands only.
- Fix real code or test regressions without skipping, disabling, or weakening
  the CI checks.
- Run focused verification for the affected files and document that broader
  AWF/GitHub validation is owned by AWF after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Inspect failing log excerpts and current PR check status with `gh`.
2. Run the failing unit-test node ids locally as a focused repro.
3. Inspect the implicated runtime/executor modules and tests.
4. Apply the smallest behavior/test fixes needed for the failing checks.
5. Run targeted tests for the changed behavior plus focused lint/format checks
   on changed Python files if needed.
6. Create `plans/PR349_CI_CURRENT_FIX_VALIDATION.md` with evidence and residual
   risk.
7. Commit the local fix.

## Assumptions/Changes

- The focused repro showed that several failures were stale test fixtures after
  validation cleanup started verifying HEAD after rollback/cleanup. The fix
  updates those fakes to provide the required `rev-parse` evidence instead of
  changing production cleanup behavior.
- The line-limit failure is addressed by mechanically splitting oversized test
  modules into smaller focused modules; no quality gate or workflow
  configuration is changed.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <targeted failing node ids> -q`
  passes for the observed failing tests.
- Focused lint/format checks for changed Python files pass if Python files are
  modified.
- `gh pr checks 349 --repo dimileeh/aira-agent-workspace-fabric` is rechecked
  for current status, but broad/full CI remains AWF/GitHub-owned.
