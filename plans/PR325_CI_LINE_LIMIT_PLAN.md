# PR325 CI Line Limit Plan

## Problem Statement

PR #325 is failing the Python full coverage job because the maintainability guard
`test_first_party_code_files_stay_under_line_limit` found first-party files over
the 1,500 line limit:

- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`

Scope is limited to decomposing these files without weakening the guard or
changing runtime behavior.

## Requirements Checklist

- Keep all first-party source and test files at or below 1,500 lines.
- Preserve the existing `awf.runtime.pr_monitor_runner.helpers` import surface
  used by runtime modules and tests.
- Move behavior mechanically; do not change PR monitor decisions or test
  assertions.
- Run the provided focused repro before and after the fix.
- Avoid broad AWF/GitHub-owned validation locally; AWF/GitHub CI handles full
  coverage and required-check provenance after agent completion.

## Implementation Steps

1. Extract the non-check reviewer settle helper cluster from
   `helpers.py` into a focused module and re-export those helpers from
   `helpers.py`.
2. Remove imports from `helpers.py` that only the extracted module needs.
3. Split a small, coherent group of tests from
   `test_pr_monitor_runner_part_001.py` into a new runner part file.
4. Run the focused maintainability repro and targeted tests for the extracted
   runtime helper surface.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py -q`

Pass criteria: the focused line-limit gate passes, extracted helper behavior
tests pass, and the affected runner part tests pass. Full AWF/GitHub validation
is intentionally left to AWF after this agent phase.
