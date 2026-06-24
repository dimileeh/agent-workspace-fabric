# CI Status Part Line Limit Validation

Plan reference: `plans/CI_STATUS_PART_LINE_LIMIT_PLAN.md`

## Requirement Status

- Reproduce the failing maintainability check locally: Complete.
  `test_first_party_code_files_stay_under_line_limit` failed with
  `test_status_part_001.py` at 1544 lines.
- Move a cohesive group of tests out of the oversized status test part:
  Complete. Stranded-workspace status tests were moved to
  `test_status_part_003.py`.
- Preserve the moved test behavior and assertions: Complete. The moved tests
  were not behaviorally changed.
- Keep every first-party code file at or below the 1500-line limit: Complete.
  `test_status_part_001.py` is now 1360 lines and the new part is 205 lines.
- Run focused validation only: Complete. Full AWF/GitHub validation, full unit
  shards, and coverage gates were not run in the agent phase because AWF owns
  those after completion.
- Commit the scoped CI fix locally: Complete.

## Evidence

Pre-edit focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result: failed because `tests/unit/service/test_status_parts/test_status_part_001.py`
had 1544 lines.

Post-edit focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_003.py -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev ruff check tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/service/test_status_parts/test_status_part_003.py
```

Results: moved tests passed (`3 passed`), line-limit check passed (`1 passed`),
and ruff reported `All checks passed!`.

## Files Changed

- `tests/unit/service/test_status_parts/test_status_part_001.py`
- `tests/unit/service/test_status_parts/test_status_part_003.py`
- `plans/CI_STATUS_PART_LINE_LIMIT_PLAN.md`
- `plans/CI_STATUS_PART_LINE_LIMIT_VALIDATION.md`
