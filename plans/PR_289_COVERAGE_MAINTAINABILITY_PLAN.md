# PR 289 Coverage And Maintainability Plan

## Problem Statement And Scope

PR #289 currently fails the `python-full-coverage` CI job. The latest CI log
shows the original agent-runtime Docker image download failure is already fixed
on this branch; the remaining failure is the full coverage job reporting:

- `test_first_party_code_files_stay_under_line_limit` found first-party files
  above the repository's 1,500 line maintainability limit.
- Total coverage was just below the 99% threshold, with missed branches in
  `src/awf/api/schemas_companions.py`.

This plan fixes those issues without weakening checks, changing workflow
configuration, pushing, rebasing, or running broad AWF/GitHub-owned validation
locally.

## Requirements Checklist

- Keep all work on the current AWF branch.
- Do not edit protected CI/workflow/quality-gate configuration.
- Split oversized first-party files below the 1,500 line limit without changing
  behavior.
- Add focused tests for the companion schema branches missed by coverage.
- Run only targeted local checks for changed behavior and the failing
  maintainability guard.
- Record validation evidence in a matching validation document.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Move GC worktree-removal helpers out of `src/awf/service/gc.py` into a small
   focused service module, while preserving the existing names imported by
   tests and callers.
2. Move a large block of GC worktree-removal tests from
   `tests/unit/service/test_gc_more2.py` into a dedicated test module.
3. Move provisioner failure-handling tests from part 001 into part 002 so both
   generated-style test parts stay under the line limit.
4. Move one workspaces observability recovery summary test from part 001 into
   part 003 to clear the line limit.
5. Add focused companion schema tests covering trailing dollar signs, escaped
   dollar signs, interpolation rejection, non-interpolation dollar usage,
   non-default repo-relative paths, and invalid environment keys.
6. Run focused tests and lint for the touched files only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes.
- Focused pytest commands for changed test modules pass.
- `uv run --python 3.12 --extra dev ruff check <touched files>` passes.
- Full AWF/GitHub validation is not run locally; AWF owns the broad post-agent
  validation and CI provenance.
