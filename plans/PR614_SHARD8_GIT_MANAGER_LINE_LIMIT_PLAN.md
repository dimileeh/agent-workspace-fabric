# PR614 Shard 8 Git Manager Line Limit Plan

## Problem Statement and Scope

The current PR #614 CI run `27838374984` fails in `python-coverage-shards (8)`.
The failing test is
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
It reports `tests/unit/node/test_git_manager.py` at 1512 lines, above the
1500-line first-party file limit.

Scope is limited to preserving the recently added git-manager behavior coverage
while moving enough tests into a sibling module to satisfy the line limit.

## Requirements Checklist

- Keep all behavior assertions; do not delete coverage to satisfy the limit.
- Do not edit CI, workflow, quality-gate, or threshold configuration.
- Keep changes limited to affected tests and plan/validation docs.
- Run focused pytest commands for the line-limit check and moved tests only.
- Record validation evidence and leave broad AWF/GitHub validation to AWF/CI.

## Implementation Steps

1. Confirm the local line-limit failure for `test_git_manager.py`.
2. Move the git-manager ownership/cleanup edge tests from
   `tests/unit/node/test_git_manager.py` into a new sibling test module.
3. Re-run the moved tests from their new path.
4. Re-run the focused maintainability line-limit test.
5. Save validation evidence in
   `plans/PR614_SHARD8_GIT_MANAGER_LINE_LIMIT_VALIDATION.md`.
6. Commit the scoped fix locally with a conventional CI-fix message.
