# Comment 3335511834 Literal Ignored Paths Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GLTsL` reports that validation ignored-path
snapshots pass ignored roots from `git status` directly to `git ls-files`.
When an ignored root is also valid Git pathspec-magic syntax as a literal
filename, such as `:(glob)cache/`, Git interprets it as a pathspec instead of
the literal root and returns an incomplete ignored baseline snapshot.

Scope is limited to validation worktree ignored snapshot probing in
`src/awf/runtime/validation_worktree.py` and focused regression coverage.

## Requirements Checklist

- Use literal pathspec handling for ignored snapshot `git ls-files` commands.
- Add a regression test covering a literal ignored root named `:(glob)cache/`.
- Preserve existing cleanup behavior for tracked restore and generated ignored
  artifact removal.
- Run focused validation only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a focused failing regression test for a real Git worktree with an
   ignored literal pathspec-magic root.
2. Update ignored snapshot command construction to invoke `git ls-files` with
   literal pathspec semantics.
3. Adjust existing unit-test command expectations for the updated snapshot
   command shape.
4. Run the new regression test, then the relevant validation worktree unit
   tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_snapshots_pathspec_magic_ignored_root_literally -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  passes after implementation.
- Full AWF/GitHub validation is not run locally per workspace contract.
