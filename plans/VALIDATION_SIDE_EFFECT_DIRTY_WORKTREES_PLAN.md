# Validation Side-Effect Dirty Worktrees Plan

## Problem

AWF monitor and executor validation commands can mutate tracked generated files
inside the managed worktree. When those validation side effects are left dirty,
the next PR-monitor repair correctly refuses to start with
`PRE_EXISTING_DIRTY_WORKTREE`, even though the dirty state was created by AWF
rather than by the agent or user.

## Scope

- Add a shared guard for AWF-owned validation worktree cleanliness.
- Before validation starts, fail immediately if the worktree is already dirty.
- After validation or coverage commands finish, restore any validation-created
  dirty worktree state to the pre-validation head and verify the cleanup.
- Use the guard from PR-monitor pre-push validation and the main executor
  validation loop.
- Preserve the existing strict repair-start dirty guard.

## Requirements

- Passing monitor pre-push validation that dirties a tracked generated file must
  clean it and still push.
- Monitor pre-push validation must fail before validation if the worktree starts
  dirty.
- If monitor validation fails and dirties the tree, cleanup must happen before a
  validation-fix agent pass can run.
- Cleanup failure must block push with structured paths and reason code.
- Executor validation must clean validation-created dirt before PR creation.
- Validation-fix agent commits must not include AWF validation side effects.
- If a PR-monitor validation-fix agent edits files but AWF cannot commit those
  edits, AWF must roll back the local fix-pass delta before the monitor loops
  again. A failed commit attempt must not leave staged or unstaged repair files
  behind for the next comment/CI repair turn.

## Implementation

- Introduce a small runtime helper module for dirty detection and validation
  side-effect cleanup using `git status --porcelain`,
  `git restore --source <pre-validation-head> --staged --worktree -- <paths>`,
  and `git clean -fd -- <paths>` for untracked files.
- Add reason codes for pre-existing validation dirt and cleanup failure.
- In PR-monitor pre-push validation, run the pre-check before creating a
  validation run, and run cleanup after every validation or coverage attempt
  before returning or invoking a fix pass.
- In executor validation, wrap each validation pass with the same guard and
  fail the validation operation immediately when cleanup cannot restore a clean
  worktree.
- In PR-monitor validation-fix passes, capture the fix-pass start HEAD before
  invoking the agent. On agent cleanup/exception or failed dirty-worktree commit,
  run an AWF-owned `git reset --hard <fix-start-head>` plus `git clean -fd`,
  log the rollback result, and return a fix failure without attempting push.

## Verification

- Targeted pytest for PR monitor pre-push validation.
- Targeted pytest for executor validation side-effect cleanup.
- `ruff` and `mypy` on touched Python files.
