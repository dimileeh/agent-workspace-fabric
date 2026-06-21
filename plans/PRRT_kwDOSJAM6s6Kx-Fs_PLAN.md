# PRRT_kwDOSJAM6s6Kx-Fs Report Parent Cleanup Plan

## Problem

An unresolved PR #608 review thread reports that post-validation conformance
cleanup removes the report file but can leave newly-created empty parent
directories behind. Git changed-path detection omits empty directories, so the
report path can look clean while later validation worktree snapshots still see
pre-existing dirty residue.

## Scope

Limit changes to conformance report cleanup behavior in
`src/awf/control/executor/planning_conformance.py`, focused regression tests,
and this plan/validation evidence.

## Requirements

- Remove empty parent directories after report-path cleanup, stopping at the
  worktree root.
- Preserve non-empty parent directories.
- Keep existing behavior for report files, missing paths, and report-path
  directories.
- Run only focused tests/checks for the touched behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a focused regression test showing that removing a report file also
   removes empty parent directories created only for that report.
2. Update `_remove_report_worktree_path` to accept the worktree root and remove
   empty ancestors after the report path is removed.
3. Update existing helper call sites and focused helper tests.
4. Run the narrow targeted tests and focused lint for touched files.
