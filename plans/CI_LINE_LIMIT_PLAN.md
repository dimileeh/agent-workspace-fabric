# CI Line Limit Plan

## Problem Statement and Scope

PR #325 fails the Python CI coverage job because
`test_first_party_code_files_stay_under_line_limit` finds oversized
first-party test files under
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/`.

Scope is limited to decomposing those oversized test modules without weakening
the maintainability guard, changing protected workflow/configuration files, or
altering runtime behavior.

## Requirements Checklist

- Keep every first-party code file at or below the 1,500-line guardrail.
- Preserve the existing PR monitor runner edge test coverage.
- Do not disable, skip, or weaken the maintainability check.
- Do not edit protected workflow, quality-gate, or configuration files.
- Run only focused local verification; full AWF/GitHub validation remains owned
  by AWF after agent completion.
- Commit the local fix with a conventional commit message.

## Implementation Steps

1. Reproduce the reported maintainability failure with the focused pytest node.
2. Identify every oversized first-party file reported by that focused failure.
3. Split complete tests from oversized part files into new continuation part
   modules, importing only the helper symbols required by the moved tests.
4. Re-run the focused maintainability test and targeted tests for the moved
   modules.
5. Write validation evidence in `plans/CI_LINE_LIMIT_VALIDATION.md`.
6. Commit the scoped fix locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  must pass.
- Targeted pytest commands covering each moved test module must pass.
- `wc -l tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/*.py`
  must show all files at or below 1,500 lines.

Full AWF/GitHub validation is intentionally not run in this workspace because
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.
