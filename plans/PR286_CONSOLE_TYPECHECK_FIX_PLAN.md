# PR286 Console Typecheck Fix Plan

## Problem Statement and Scope

PR #286 fails the GitHub Actions `console` job during `npm --prefix apps/console run typecheck`.
The CI log and local focused repro both report:

- `apps/console/components/console-dashboard.tsx(719,14): Expected 3 arguments, but got 2.`
- `apps/console/tests/dashboard-log-viewer.spec.ts(165,11): 'seq' does not exist in type '{ type: string; workspace_id: string; }'.`

Scope is limited to fixing these TypeScript errors without changing workflow/configuration files or running broad AWF/GitHub-owned validation.

## Requirements Checklist

- Keep all work on the current AWF-managed branch.
- Do not push, rebase, switch branches, or edit protected workflow/quality-gate configuration.
- Fix the console typecheck errors without weakening the CI check.
- Preserve existing log reload behavior and SSE log frame test coverage.
- Run focused verification only and record that full AWF/GitHub validation remains owned by AWF after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Update the selected-workspace log reload call to pass the selected stream IDs expected by `loadLogTail`.
2. Type the Playwright mocked SSE frame list as `AwfStreamFrame[]` so connected and log frames are accepted by the shared stream contract.
3. Re-run `npm --prefix apps/console run typecheck` as the focused repro for the failing check.
4. Inspect the worktree for generated or unrelated changes and commit only the intended files plus the required plan/validation docs.

## Verification Commands and Pass Criteria

- `npm --prefix apps/console run typecheck`
  - Passes with no TypeScript errors.
- `git status --short`
  - Shows only the intended source, test, plan, and validation changes before commit.

Full AWF/GitHub broad validation and remaining pending CI checks are intentionally not run locally in this agent phase.
