# Monitor Dirty Recovery Plan

## Goal

Prevent PR monitor workspaces from failing permanently when a provider retry,
timeout, or monitor repair leaves operation-owned dirty worktree state behind.

## Root Causes

- `ws_80d3fed8f7ca4b7fb58573a8` and `ws_d6a832c393d0481ba1b8e065`
  failed because a provider-retry repair left uncommitted files in the repair
  worktree; the next monitor iteration refused to start with
  `PRE_EXISTING_DIRTY_WORKTREE`.
- `ws_8b7b1832a5fb434c818db526` failed because residual staged repair changes
  were still present when pre-push validation ran; validation correctly refused
  the dirty tree with `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.

## Implementation

- Before provider recovery raises from CI repair, commit safe dirty repair
  output using the existing `_commit_dirty_worktree` sink so retries do not
  inherit uncommitted agent artifacts.
- Before pre-push validation applies the clean-worktree guard, run one bounded
  finalization pass for repair-owned dirty state using `_commit_dirty_worktree`.
  If that pass commits successfully, continue to validation. If it cannot
  commit, keep the existing dirty-worktree failure.
- Keep all existing fail-closed behavior for unrelated pre-existing dirt.

## Validation

- Add regression coverage for CI provider-retry dirty output being finalized
  before the retry is raised.
- Add regression coverage for pre-push validation finalizing residual dirty
  repair output before running validation.
- Preserve existing tests proving truly pre-existing dirty worktrees remain
  terminal.
