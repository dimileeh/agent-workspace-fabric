# Remote Repair Rename Policy Validation

Plan reference: `plans/REMOTE_REPAIR_RENAME_POLICY_PLAN.md`

## Requirement Status

- Complete: Use `git diff --cached --name-status -z` for staged recovery path collection.
- Complete: Parse staged paths with the existing name-status parser so rename and copy sources are included.
- Complete: Preserve agent-runtime path exclusion before policy refresh and commit.
- Complete: Add/update focused regression coverage showing a lockfile rename source reaches `_refresh_supply_chain_policy_before_push`.
- Complete: Run only targeted local validation; full AWF/GitHub validation remains managed after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_blocks_policy_before_recovery_commit -q`
  - Failed before implementation because staged rename output was treated as raw name-only paths and included `R100`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`
  - Passed: `18 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
  - Passed.

Broad AWF/GitHub validation was not run in the agent phase per workspace contract; AWF owns the post-agent broad validation, provenance, logs, and merge gating.
