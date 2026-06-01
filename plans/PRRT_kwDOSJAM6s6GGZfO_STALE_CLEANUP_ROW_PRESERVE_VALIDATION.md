# PRRT_kwDOSJAM6s6GGZfO Stale Cleanup Row Preserve Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GGZfO_STALE_CLEANUP_ROW_PRESERVE_PLAN.md`

## Requirement Status

- Complete: Preserve `workspace.failure_reason` and
  `workspace.failure_message` when stale validation cleanup records secondary
  evidence without a loaded primary failure.
- Complete: Continue appending secondary cleanup evidence to the emitted
  `workspace.secondary_failure_recorded` payload.
- Complete: Preserve existing primary-failure restoration behavior covered by
  the adjacent stale cleanup test.
- Complete: Commit locally without pushing or switching branches.

## Evidence

Files changed:

- `src/awf/control/executor/execution_validation.py`
- `tests/unit/control/test_executor_validation_stale_cleanup.py`
- `plans/PRRT_kwDOSJAM6s6GGZfO_STALE_CLEANUP_ROW_PRESERVE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GGZfO_STALE_CLEANUP_ROW_PRESERVE_VALIDATION.md`

Failing regression before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_stale_cleanup.py::test_stale_validation_cleanup_without_primary_keeps_failed_row_fields -q
```

Result: failed because `workspace.failure_reason` changed from
`validation_failure` to `infrastructure_failure`.

Focused validation after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_stale_cleanup.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_stale_validation_cleanup_failure_records_secondary_failure_evidence -q
```

Result: passed, `2 passed in 0.88s`.

Focused lint after implementation:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_validation_stale_cleanup.py
```

Result: passed, `All checks passed!`.

Full AWF/GitHub validation was not run in the agent phase; AWF owns that broad
validation after completion.

## Gaps

None.
