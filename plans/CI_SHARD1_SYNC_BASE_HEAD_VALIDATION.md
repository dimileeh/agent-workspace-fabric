# CI shard 1 sync-base head validation

Plan reference: `plans/CI_SHARD1_SYNC_BASE_HEAD_PLAN.md`

## Requirement status

- Complete: Preserved AWF branch ownership. No branch switch, push, rebase, or
  broad AWF/GitHub validation was run.
- Complete: Diagnosed shard-1 failures from CI logs; all showed sync-base
  stopping at `REPAIR_START_HEAD_UNAVAILABLE` before merge/push behavior.
- Complete: Used the supplied PR head SHA as sync-base operation-start baseline
  when present.
- Complete: Kept the explicit rev-parse fallback for direct `_run_sync_base`
  callers without `pr_head_sha`.
- Complete: Ran focused shard-1 pytest targets and touched-file Ruff.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py`

Commands run:

- Focused nine-test shard-1 pytest command from CI failures.
  - Result: `9 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
  - Result: passed.

## Residual risk

GitHub still shows the previous failed run for shards 1, 6, and 8 because this
local fix has not been pushed by AWF yet. Aggregate `python-full-coverage` and
`ci-required` failures are downstream of those shard failures.
