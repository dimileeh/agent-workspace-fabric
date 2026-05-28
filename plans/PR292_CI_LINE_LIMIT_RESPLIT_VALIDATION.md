# PR292 CI Line Limit Resplit Validation

## Result

The implementation matches `plans/PR292_CI_LINE_LIMIT_RESPLIT_PLAN.md`.

## Evidence

Focused repro before the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Failed because `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
had 1619 lines.

Focused validation after the split:

```bash
wc -l tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py
```

Result:

```text
1490 tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py
154 tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py
```

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py -q
```

Result: `19 passed in 5.35s`.

## Notes

No broad AWF/GitHub-owned validation was run locally. Full CI and coverage
provenance remain owned by AWF/GitHub after this agent phase.
