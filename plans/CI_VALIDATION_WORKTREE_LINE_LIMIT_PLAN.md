# CI Validation Worktree Line Limit Plan

## Problem Statement and Scope

PR #606 CI fails in `python-coverage-shards (8)` because
`tests/unit/runtime/test_validation_worktree.py` has grown to 1521 lines, over
the first-party file limit of 1500 lines enforced by
`tests/unit/test_core_decomposition_maintainability.py`.

Scope is limited to reorganizing the oversized validation worktree tests without
changing production behavior or weakening the maintainability guard.

## Requirements Checklist

- Keep all existing validation worktree behavior covered.
- Reduce every first-party code file to at most 1500 lines.
- Do not edit protected workflow, quality-gate, or configuration files.
- Do not run broad AWF/GitHub-owned validation locally.
- Commit the scoped CI fix locally; do not push.

## Implementation Steps

1. Move the tail `cleanup_validation_worktree_side_effects` rollback/cleanup
   tests from `tests/unit/runtime/test_validation_worktree.py` into a sibling
   test module.
2. Reuse the existing test helpers/constants from the original module so the
   moved tests keep the same assertions and simulated git command behavior.
3. Confirm the original module falls under the 1500-line maintainability limit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_cleanup_edges.py -q`
  - Passes, proving the affected tests still execute after the split.

Full AWF/GitHub validation remains managed by AWF after agent completion.
