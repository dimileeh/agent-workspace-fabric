# CI Line Limit Validation

Plan reference: `plans/CI_LINE_LIMIT_PLAN.md`

## Requirement Status

- Keep every first-party code file at or below the 1,500-line guardrail:
  Complete.
- Preserve the existing PR monitor runner edge test coverage:
  Complete. Six moved tests were relocated into continuation modules and passed
  in their new locations.
- Do not disable, skip, or weaken the maintainability check:
  Complete. The guard was left unchanged and now passes.
- Do not edit protected workflow, quality-gate, or configuration files:
  Complete. Only test decomposition files and plan/validation docs were edited.
- Run only focused local verification:
  Complete. Broad AWF/GitHub validation was not run; AWF owns it after agent
  completion.
- Commit the local fix with a conventional commit message:
  Complete. This validation document is included in the local fix commit.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_010.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_012.py`
- `plans/CI_LINE_LIMIT_PLAN.md`
- `plans/CI_LINE_LIMIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Initial result: failed, reporting oversized part files 003, 004, and 006.
  - Final result: passed, `1 passed in 0.38s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_010.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_012.py`
  - Final result: passed, `All checks passed!`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_010.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_012.py -q`
  - Final result: passed, `6 passed in 6.26s`.
- `wc -l tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/*.py`
  - Final result: all part files were at or below 1,500 lines; edited originals
    are 1,487, 1,449, and 1,460 lines, and new continuation files are 229,
    137, and 89 lines.
- `git diff --check`
  - Final result: passed.

## Gaps

No validation gaps remain for the focused CI failure. Full AWF/GitHub validation
remains intentionally deferred to AWF after agent completion.
