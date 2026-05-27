# GC Companion Worktree Review Plan

## Problem Statement And Scope

Address PR review comment `issue:4556565363` for companion worktree GC behavior.
The scope is limited to GC diagnostics and companion worktree candidate/target
selection:

- partial git worktree-removal failures should report the failing worktree path,
  not always the primary workspace path;
- companion worktree GC path discovery and remove-target discovery should share
  the same required fields so malformed name-only companion policy entries do
  not produce unremovable blocked candidate paths.

## Requirements Checklist

- Add regression coverage for partial worktree-removal error attribution.
- Add regression coverage for malformed companion policy entries with `name` but
  no `repo_url`.
- Preserve existing per-target deletion decisions and path outcomes.
- Keep the change scoped to GC companion worktree handling.
- Do not run broad AWF/GitHub-owned validation; record focused checks only.

## Implementation Steps

1. Update focused GC tests to assert `delete_errors` uses the failed companion
   path for partial worktree-removal failures.
2. Add a focused GC test for name-only companion policy entries.
3. Update GC delete-error construction to derive worktree-remove diagnostics
   from failed per-target results when available.
4. Align companion worktree path discovery with remove-target discovery by
   requiring both `name` and `repo_url`.
5. Run the focused GC regression tests touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q`
  should pass.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
