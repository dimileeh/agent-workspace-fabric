# PR329 name-only -z parser validation

Plan reference: `plans/PR329_NAME_ONLY_Z_PLAN.md`

## Requirement status

- Verify whether the current parser accepts non-NUL and unterminated output:
  Complete. The new regression failed before implementation because both
  malformed cases did not raise `ProtectedScopeDiffError`.
- Add regression coverage showing malformed `--name-only -z` output fails
  closed: Complete. Added focused malformed-output coverage for non-NUL,
  missing-terminator, and empty-path records.
- Preserve valid behavior for empty output, valid NUL-terminated paths,
  duplicate paths, and empty path rejection: Complete. Empty output remains an
  immediate `()`, valid NUL-terminated paths are deduplicated, and empty path
  rejection is covered.
- Raise `ProtectedScopeDiffError` for malformed `--name-only -z` output:
  Complete. `_changed_paths_from_name_only_z` rejects non-NUL and unterminated
  output with explicit parser errors.
- Keep validation focused; AWF/GitHub own broad validation after agent
  completion: Complete. Targeted local tests and lint were run intentionally.
  The commit hook also ran its configured checks and initially blocked on
  formatter drift; full AWF/GitHub validation remains owned by AWF.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/path_helpers.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
- `plans/PR329_NAME_ONLY_Z_PLAN.md`
- `plans/PR329_NAME_ONLY_Z_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py -q -k name_only_z`
  - Pre-implementation result: failed with 2 malformed-output cases not raising
    `ProtectedScopeDiffError`.
  - Post-implementation result: 4 passed, 30 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py -q`
  - Result: 34 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/path_helpers.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format src/awf/runtime/pr_monitor_runner/path_helpers.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
  - Result: 2 files reformatted after the commit hook reported formatter
    drift.

Full AWF/GitHub validation was not run during this agent phase; AWF owns broad
validation and merge gating after agent completion.
