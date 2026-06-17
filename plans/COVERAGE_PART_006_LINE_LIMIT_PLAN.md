# Plan: Bring `test_pr_monitor_runner_coverage_edges_part_006.py` under first-party line limit

## Problem Statement

CI run `python-coverage-shards (8)` for PR #613 failed with:

```
AssertionError: assert {'tests/unit/...006.py': 1645} == {}
```

`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py` reports 1,645 splitlines, which exceeds the first-party 1,500-line maintainability limit enforced by `tests/unit/test_core_decomposition_maintainability.py` (`MAX_FIRST_PARTY_FILE_LINES = 1_500`).

The previous local commit `f34888032` attempted to split the file by moving 517 lines into the newly-created `test_pr_monitor_runner_coverage_edges_part_018.py`, but the remaining `part_006.py` still has 1,186 physical / 1,187 counted lines—wait, that is under 1,500. The CI failure excerpt showed `1645`, which corresponds to the pre-split state. The actual current state after the split must be re-verified locally and, if still over the limit, reduced.

## Requirements

- [ ] Verify current line count of `part_006.py` against the 1,500-line limit.
- [ ] If still over, split/move tests out of `part_006.py` into a new or existing part file so **every** file in `test_pr_monitor_runner_coverage_edges_parts/` is <= 1,500 lines.
- [ ] Preserve all existing tests, imports, fixtures, and helpers exactly as-is (move whole functions, do not edit assertions).
- [ ] Keep the new split file under the same 1,500-line limit.
- [ ] Run the decomposition maintainability test and the touched part file(s) to confirm no regression.
- [ ] Avoid broad validation; do not run the full coverage gate locally.

## Implementation Steps

1. Check current line counts and identify the safe split point in `part_006.py`.
2. If needed, create `test_pr_monitor_runner_coverage_edges_part_019.py` containing a contiguous block of tests moved from the end of `part_006.py`, plus the required imports/fixtures shared with the moved tests (or rely on module-level imports if they already cover it).
3. Update `part_006.py` to remove the moved block, removing now-unused imports only if they become unused.
4. Re-run `pytest tests/unit/test_core_decomposition_maintainability.py` and the relevant part files.

## Verification

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q
```

Pass criteria:
- `test_first_party_code_files_stay_under_line_limit` passes.
- All moved tests pass.
- No new first-party file exceeds 1,500 lines.
