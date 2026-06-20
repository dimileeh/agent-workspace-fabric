# CI Shard 8 Line Limit Validation

Plan reference: `CI_SHARD8_LINE_LIMIT_PLAN.md`

## Requirement Status

- Complete: Keep all files under the repo's first-party line limit.
- Complete: Preserve the behavior covered by the moved tests.
- Complete: Do not edit workflow, quality-gate, or protected configuration files.
- Complete: Preserve the SyncBase protected-scope pause behavior: protected
  violations during base-conflict resolution leave the workspace blocked, not
  failed.
- Complete: Run focused validation only; AWF/GitHub own broad validation after
  agent completion.
- Complete: Commit the local fix on the current AWF branch without pushing.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_022.py`
- `plans/CI_SHARD8_LINE_LIMIT_PLAN.md`
- `plans/CI_SHARD8_LINE_LIMIT_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Pass: `1 passed in 0.45s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py::test_execute_sync_base_protected_violation_pauses_into_blocked_not_terminal -q`
  - Initial repro failed with `ws.status == "failed"` instead of `"blocked"`.
  - Pass after fixture repair: `1 passed in 2.28s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py -q`
  - Pass: `6 passed in 8.03s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_022.py -q`
  - Pass: `25 passed in 21.11s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_022.py`
  - Pass: `All checks passed!`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_022.py -q`
  - Pass: `32 passed in 27.62s`

Full AWF/GitHub validation was not run locally per the workspace contract; AWF
owns broad validation, provenance, logs, timeouts, and merge gating after agent
completion.
