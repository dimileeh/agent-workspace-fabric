# PRRT_kwDOSJAM6s6DaUmx Plan

## Problem Statement And Scope

An unresolved PR review thread reports that exceptions from the preserved-active
branch open-PR resolver are treated as ambiguous PR lookup results. That writes
an operator-required salvage event, and later scans skip automated recovery
because the operator-required event is considered current.

Scope is limited to preserved active execution recovery in
`src/awf/control/worker.py` and the focused unit coverage around that salvage
path.

## Requirements Checklist

- Resolver exceptions must not create an ambiguous branch lookup result.
- Resolver exceptions must allow recovery to continue to worktree
  classification.
- Genuine ambiguous resolver results, such as multiple open PR matches, must
  still require operator recovery.
- Add or update regression coverage for the resolver-exception fallback.
- Preserve narrow validation with the relevant worker unit tests.

## Implementation Steps

1. Add a failing unit regression for a preserved pushing workspace where open PR
   lookup raises and committed worktree salvage still requests validation.
2. Update existing ambiguity coverage so it continues to assert true ambiguous
   resolver results without expecting transient resolver exceptions to be
   operator-required.
3. Change `_resolve_preserved_active_branch_open_pr` to return `None` when the
   resolver raises, allowing the caller to continue to worktree classification.
4. Run the focused test(s), then the broader worker unit test module if
   practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes.
- If runtime is constrained, at minimum run the focused tests covering
  preserved-active branch lookup and document the gap.
