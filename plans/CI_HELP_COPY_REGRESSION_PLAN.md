# CI Help Copy Regression Plan

## Problem Statement And Scope

PR #296 still fails the GitHub `python-full-coverage` job at head
`237b873224c4bf48716265ce193ab58422ca6c93`. The previous line-limit failure is
fixed, but the full-suite run still fails three CLI help regression tests that
expect `awf init <path>` to be visible in init/setup/start help output.

This fix is scoped to preserving the public first-run CLI grammar and making the
help regression deterministic under full-suite execution. It must not weaken the
CI check, skip tests, or change protected workflow/quality-gate configuration.

## Requirements Checklist

- [ ] Reproduce the CI help-copy failure with a focused local selection.
- [ ] Identify why the help output differs between isolated focused tests and
      the full coverage run.
- [ ] Preserve public help guidance that points project onboarding at
      `awf init <path>`.
- [ ] Keep the existing line-limit split intact.
- [ ] Add or update focused regression coverage for the fix.
- [ ] Run focused verification commands only; leave full coverage to AWF/GitHub.

## Implementation Steps

1. Inspect the failing help tests and CLI help configuration.
2. Search for order-dependent Rich/Typer help rendering state or environment
   mutation that can hide `awf init <path>`.
3. Reproduce the hidden-help behavior locally with the narrowest practical test
   selection.
4. Patch the smallest code or test helper surface that makes the public help
   text deterministic while preserving the documented CLI contract.
5. Run the reported failing nodes and any added focused regression tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused-repro> -q`
  - Passes and reproduces the previously failing help output before the fix when
    practical.
- `uv run --python 3.12 --extra dev pytest -n 8 --dist=loadscope --timeout=300 tests/unit/cli/test_init_parts/test_init_part_001.py::test_init_help_documents_project_onboarding_and_new_first_run_flow tests/unit/cli/test_setup_commands.py::test_setup_help_describes_first_run_surface tests/unit/cli/test_start_commands.py::test_start_help_describes_local_core_surface -q`
  - All three help-copy tests pass under the CI xdist shape.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_005.py -q`
  - The existing line-limit split shard remains green.

Full AWF/GitHub coverage validation remains managed by AWF after agent
completion.
