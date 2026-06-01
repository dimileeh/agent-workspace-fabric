# CI Validation Cleanup Fix Validation

Plan reference: `plans/CI_VALIDATION_CLEANUP_FIX_PLAN.md`

## Requirement Status

- Preserve primary validation failure causality when stale validation cleanup
  records secondary cleanup evidence: Complete.
  - Evidence: stale-cleanup persistence regressions pass against
    `validation_cleanup_guards._record_stale_validation_cleanup_failure`.
- Keep unit-level monkeypatch seams compatible with the decomposed cleanup guard:
  Complete.
  - Evidence: tests now patch `validation_cleanup_guards.WorkspaceRepository`,
    `validation_cleanup_guards.load_failure_causality_snapshot`, and
    `validation_cleanup_guards._record_stale_validation_cleanup_failure`.
- Reduce first-party test files below the configured line limit without deleting
  behavioral coverage: Complete.
  - Evidence: signature/hash tests moved to
    `tests/unit/runtime/test_validation_worktree_signatures.py`; line counts are
    1451 for `tests/unit/runtime/test_validation_worktree.py` and 74 for the new
    signature test file.
- Do not disable, skip, or weaken maintainability, coverage, or CI checks:
  Complete.
  - Evidence: no quality-gate or workflow configuration was changed.
- Run only focused repro/verification commands locally: Complete.
  - Evidence: all commands below are targeted to the CI failure or touched files.
- Commit the fix locally on the current AWF-managed branch: Complete.
  - Evidence: included in the local fix commit after this validation document
    was written.

## Evidence

Focused CI repro:

```bash
uv run --python 3.12 --extra dev pytest \
  'tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception[callback-already-stale]' \
  'tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception[callback-becomes-stale]' \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_stale_validation_cleanup_failure_records_secondary_failure_evidence \
  tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit \
  tests/unit/control/test_executor_validation_stale_cleanup.py::test_stale_validation_cleanup_without_primary_keeps_failed_row_fields \
  -q
```

Result: `5 passed in 1.53s`.

Moved/touched runtime tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_validation_worktree.py \
  tests/unit/runtime/test_validation_worktree_signatures.py \
  -q
```

Result: `39 passed in 1.23s`.

Focused style check:

```bash
uv run --python 3.12 --extra dev ruff check \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py \
  tests/unit/control/test_executor_validation_stale_cleanup.py \
  tests/unit/runtime/test_validation_worktree.py \
  tests/unit/runtime/test_validation_worktree_signatures.py \
  plans/CI_VALIDATION_CLEANUP_FIX_PLAN.md
```

Result: `All checks passed!`.

Line count check:

```bash
wc -l tests/unit/runtime/test_validation_worktree.py \
  tests/unit/runtime/test_validation_worktree_signatures.py
```

Result: `1451` and `74` lines respectively.

Full coverage, whole-repository pytest, frontend builds, and CI-equivalent
validation were not run locally because AWF/GitHub owns broad validation after
the agent phase.
