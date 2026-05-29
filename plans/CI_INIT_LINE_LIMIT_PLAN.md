# CI Init Line-Limit Fix Plan

## Problem Statement And Scope

PR #296 fails the focused CI repro because
`tests/unit/cli/test_init_parts/test_init_part_001.py` has grown past the
repository maintainability guardrail of 1,500 lines. The same focused repro now
shows the init/setup/start help text assertions passing locally, so this fix is
scoped to preserving the existing init coverage while bringing every first-party
code file under the line limit.

## Requirements Checklist

- Keep all existing init CLI behavior assertions covered.
- Bring `test_init_part_001.py` under `MAX_FIRST_PARTY_FILE_LINES`.
- Keep any new first-party test file under `MAX_FIRST_PARTY_FILE_LINES`.
- Do not weaken, skip, or disable the maintainability check.
- Run focused verification only; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Move a cohesive tail group of no-path bootstrap env-merging tests from
   `test_init_part_001.py` into a new split module.
2. Add only the imports and local helper doubles needed by the moved tests.
3. Verify line counts and run the AWF-provided focused pytest repro.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
  - Passes with all four focused nodes green.
- Targeted moved-test command for the new split module passes.
- `wc -l` confirms touched split test files are under 1,500 lines.
