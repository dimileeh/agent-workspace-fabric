# PR #673 CI Coverage Fix Validation

Plan reference: `plans/PR673_CI_COVERAGE_FIX_PLAN.md`

## Requirement Status

- Identify the coverage miss from CI logs before changing code: Complete.
  - CI `python-full-coverage` failed at combined line+branch coverage `98.996%`
    below the `99.00%` requirement.
  - The CI missing-lines report showed an owned reachable gap in
    `src/awf/runtime/pr_monitor_runner/gates.py`, including lines `487-496`.
- Add a meaningful focused test for reachable behavior in an owned changed area:
  Complete.
  - Added
    `test_stale_gate_handler_records_ignored_callback_for_terminal_workspace`
    in
    `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py`.
  - The test asserts terminal workspaces do not dispatch stale recovery
    operations and instead record `workspace.stale_callback_ignored`.
- Keep changes minimal and avoid weakening or skipping checks: Complete.
  - No workflow, threshold, or production behavior changes were made.
- Run targeted validation only for the changed tests/module: Complete.
  - Focused commands and results are listed below.
- Record validation evidence and note broad validation ownership: Complete.
  - Full AWF/GitHub validation and the full coverage gate were not run locally;
    AWF and GitHub CI own broad validation after agent completion.

## Evidence

Files changed:

- `plans/PR673_CI_COVERAGE_FIX_PLAN.md`
- `plans/PR673_CI_COVERAGE_FIX_VALIDATION.md`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py::test_stale_gate_handler_records_ignored_callback_for_terminal_workspace -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev coverage run --branch -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_013.py::test_stale_gate_handler_records_ignored_callback_for_terminal_workspace -q && uv run --python 3.12 coverage report --include=src/awf/runtime/pr_monitor_runner/gates.py --show-missing`
  - Test passed, then the report command exited with the repository-level
    `fail-under=99.00` because this was intentionally a one-test targeted
    coverage run.
- `uv run --python 3.12 --extra dev coverage report --include=src/awf/runtime/pr_monitor_runner/gates.py --show-missing --fail-under=0`
  - Passed and showed `gates.py` missing ranges no longer include lines
    `487-496`; the remaining missing range resumes at `497`.

## Gaps

None for the saved plan. The full coverage gate is deferred to AWF/GitHub CI by
workspace contract.
