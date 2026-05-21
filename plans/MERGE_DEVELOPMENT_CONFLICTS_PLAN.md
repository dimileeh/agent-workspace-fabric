# Merge Development Conflicts Plan

## Problem Statement and Scope

Resolve the `origin/development` merge conflicts left in this AWF workspace for PR #272 while preserving the intent of both the feature branch and the base branch. The scope is limited to the currently unmerged files plus this plan/validation record.

## Requirements Checklist

- Resolve conflicts in `src/awf/control/worker.py`.
- Resolve conflicts in `src/awf/db/repositories.py`.
- Resolve conflicts in `tests/unit/control/test_executor_coverage_edges.py`.
- Resolve conflicts in `tests/unit/control/test_quality_gates.py`.
- Preserve both sides where compatible, preferring base-branch semantics when intent is unclear.
- Confirm there are no remaining conflict markers or unmerged paths.
- Run the narrowest practical validation for the touched control-plane tests.
- Commit the merge resolution locally without pushing.

## Implementation Steps

1. Inspect each conflicting hunk and related nearby code.
2. Resolve imports, constants, repository methods, and test expectations in place.
3. Run targeted formatting/lint or tests that cover the resolved files.
4. Stage the resolved files and plan artifacts.
5. Commit with `chore: merge origin/development into feature branch`.

## Verification Commands and Pass Criteria

- `rg -n "<<<<<<<|=======|>>>>>>>" src/awf/control/worker.py src/awf/db/repositories.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_quality_gates.py` returns no matches.
- `git diff --name-only --diff-filter=U` returns no paths.
- Targeted `uv run --python 3.12 --extra dev pytest ...` passes for the touched tests, or any environment limitation is documented in validation.
