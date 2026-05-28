# PR292 CI Line Limit Resplit Plan

## Context

The latest focused repro for PR #292 fails:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

The failure reports `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
at 1619 lines, above the repository's 1500-line maintainability guard.

## Plan

1. Split a small, behavior-preserving group of tests from
   `test_executor_error_paths_part_006.py` into a new
   `test_executor_error_paths_part_011.py` file that follows the existing
   part-file helper import pattern.
2. Keep shared fixtures/helpers in part 006 so existing part files that import
   them remain stable.
3. Run focused validation only:
   - the line-limit maintainability test
   - the affected part 006 tests
   - the new part 011 tests
4. Record focused validation evidence in a matching validation document.

Full AWF/GitHub validation remains owned by AWF after this agent phase.
