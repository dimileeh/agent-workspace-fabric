# Validation: `test_pr_monitor_runner_coverage_edges_part_006.py` line limit

## Plan Reference

`plans/COVERAGE_PART_006_LINE_LIMIT_PLAN.md`

## Requirement Status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | Verify current line count of `part_006.py` against the 1,500-line limit. | Complete | `splitlines` count = 1,186 for `part_006.py`; all part files are `<= 1,500` lines. |
| 2 | Split/move tests if any file is `> 1,500` lines. | N/A | No oversized part file after the existing split in commit `f34888032`. |
| 3 | Preserve all tests and imports. | Complete | No test code changed; `part_006.py` and `part_018.py` retain their tests/fixtures. |
| 4 | Keep every part file under 1,500 lines. | Complete | Largest part file is 1,500 lines (`part_002.py`). |
| 5 | Run focused decomposition + part tests. | Complete | See commands below. |

## Commands Run

```bash
# Reproduce the maintainability gate locally
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
# -> 9 passed

# Verify the previously-failing part files still pass
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py -q
# -> 25 passed
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_018.py -q
# -> 13 passed

# Sanity-check lint/format
uv run --python 3.12 --extra dev ruff check src/awf tests
# -> All checks passed
uv run --python 3.12 --extra dev ruff format --check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_018.py
# -> 2 files already formatted
```

## Line Count Summary

| File | splitlines |
|------|------------|
| `test_pr_monitor_runner_coverage_edges_part_006.py` | 1,186 |
| `test_pr_monitor_runner_coverage_edges_part_018.py` | 788 |
| Largest in directory (`part_002.py`) | 1,500 |
| All others | `<= 1,500` |

## Conclusion

The local branch already contains the fix that resolves the CI failure: `part_006.py` was split into `part_006.py` (remaining tests) and `part_018.py` (moved tests) by commit `f34888032`. The failing CI run `27666687029` tested the pre-split commit. No additional code changes are required to satisfy the first-party line-limit check.

The plan was created to document the diagnosis; no implementation iteration was necessary because the root cause was a stale CI run on the pre-split commit.

## Note on Validation Documents

Per `plans/PLAN_EXECUTION_PROTOCOL.md`, a validation file is created for every non-trivial investigation. This file records that no code changes were needed beyond what is already committed.
