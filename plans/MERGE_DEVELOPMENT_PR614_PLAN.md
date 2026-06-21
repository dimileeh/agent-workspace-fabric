# Merge Development PR614 Plan

## Problem Statement And Scope

Resolve the merge conflicts left by `git merge origin/development` for PR #614 on the current AWF-managed branch. Keep the fix limited to the conflicted PR monitor runner files and the directly conflicted tests. Do not switch branches, push, rebase, or run AWF-owned broad validation.

## Requirements Checklist

- Preserve the intent of both the current PR branch and `origin/development` in each conflicted file.
- Prefer `origin/development` semantics for ambiguous hunks.
- Remove all conflict markers from the conflicted files.
- Keep existing unrelated worktree changes intact.
- Run focused checks only for the touched PR monitor runner tests.
- Stage the resolved files and commit locally with a conventional merge-resolution message.

## Implementation Steps

1. Inspect stage 2 (`HEAD`) and stage 3 (`origin/development`) versions for each conflicted file.
2. Resolve import and implementation conflicts by combining compatible changes and preserving base behavior where intent is unclear.
3. Resolve test conflicts by keeping applicable tests from both sides and adjusting imports/assertions to match the merged implementation.
4. Search the resolved files for conflict markers.
5. Run focused pytest commands for the conflicted test files.
6. Write validation notes, stage touched files, and commit locally.

## Verification Commands And Pass Criteria

- `rg -n "<<<<<<<|=======|>>>>>>>" <resolved files>` returns no matches.
- `uv run --python 3.12 --extra dev pytest <focused conflicted test files> -q` passes, or any failure is documented if unrelated to the merge resolution.
- `git status --short` shows no unmerged paths before committing.
