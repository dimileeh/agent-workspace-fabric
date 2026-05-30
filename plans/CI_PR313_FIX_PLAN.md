# CI PR 313 Fix Plan

## Problem Statement And Scope

PR #313 fails the Python full coverage job on a focused set of pytest nodes:

- AWF plan artifact paths under `docs/awf-plans/` are reported as blocking stale overlaps instead of advisory stale reasons.
- `run_validation_and_fix_cycle` has a unit fixture that no longer provides the resolved-profile sync dependency used before validation rechecks.
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py` exceeds the first-party line limit.

Scope is limited to fixing those failures without changing branch management, pushing, weakening checks, or running AWF/GitHub-owned broad validation locally.

## Requirements Checklist

- Preserve AWF plan/conformance artifacts under `docs/awf-plans/ws_*` as advisory stale reasons when they overlap owned paths.
- Keep real source overlaps blocking when plan-artifact overlaps are mixed with source path overlaps.
- Preserve validation-phase status recheck behavior while updating the focused unit fixture for resolved-profile sync.
- Split oversized first-party test code into smaller modules without changing test behavior.
- Run only focused repro, targeted tests/lint, and record that full AWF/GitHub validation is handled after agent completion.
- Commit all local changes on the current branch and do not push.

## Implementation Steps

1. Update internal plan artifact path recognition so `docs/awf-plans/ws_*` plan/conformance filenames with supported suffixes classify as internal artifacts.
2. Update the stale validation recheck unit fixture to provide the resolved-profile sync persistence dependency expected by current execution flow.
3. Move transition/event repository tests from `test_workspace_repository_part_002.py` into a new part file with local imports and fixture, keeping each first-party file below the line limit.
4. Run the AWF-provided focused pytest command again.
5. Run targeted maintainability and lint checks for changed files only.
6. Write `plans/CI_PR313_FIX_VALIDATION.md` with requirement-by-requirement evidence.
7. Commit the fix locally with a conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <AWF-provided failing node IDs> -q`
  - Pass criteria: all five previously failing focused tests pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Pass criteria: no oversized first-party files are reported.
- `uv run --python 3.12 --extra dev ruff check <changed Python files>`
  - Pass criteria: changed Python files pass targeted lint.

Full AWF/GitHub validation, coverage gates, and broad CI-equivalent runs are intentionally left to AWF after agent completion per the workspace contract.
