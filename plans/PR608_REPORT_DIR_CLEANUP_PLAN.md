# PR608 Report Directory Cleanup Plan

## Problem

An unresolved PR #608 review thread reports that post-validation conformance cleanup can return success when the conformance report path is an untracked empty directory. `git status`-based changed path detection omits empty directories, so the later validation dirty guard may still fail.

## Scope

Limit changes to `src/awf/control/executor/planning_conformance.py`, focused regression tests, and this plan/validation evidence.

## Requirements

- Verify leftover directories at the conformance report path cannot be treated as clean.
- Remove an empty directory at the report path when file unlink cleanup hits `IsADirectoryError`.
- Preserve failure behavior for unremovable or non-empty report-path directories.
- Run only focused checks for the touched behavior; broad AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused regression test for an empty directory at the report path.
2. Update cleanup to remove an empty directory when `unlink()` reports the path is a directory.
3. Update the dirty check to treat a remaining directory at the report path as dirty even when git changed-path output is empty.
4. Run the narrow targeted test file or selected test(s) that cover this behavior.
