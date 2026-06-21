# Remote Repair Rename Policy Plan

## Problem Statement And Scope

Missing-HEAD filesystem recovery stages the repaired worktree and refreshes supply-chain policy from staged paths before committing. The staged diff currently uses `git diff --cached --name-only -z`, which omits rename sources. A recovered rename from an unowned lockfile path to a non-lockfile destination can bypass the policy refresh.

Scope is limited to the missing-HEAD recovery staged-diff path in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused unit tests for that behavior.

## Requirements Checklist

- Use `git diff --cached --name-status -z` for staged recovery path collection.
- Parse staged paths with the existing name-status parser so rename and copy sources are included.
- Preserve agent-runtime path exclusion before policy refresh and commit.
- Add or update focused regression coverage showing a lockfile rename source reaches `_refresh_supply_chain_policy_before_push`.
- Run only targeted local validation; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Update the existing recovery policy-block test to script a staged rename from `package-lock.json` to `docs/notlock.txt` and assert both paths are passed to policy refresh.
2. Update existing recovery test fixtures that script staged diff output so they use name-status-shaped output.
3. Replace the staged `--name-only` recovery diffs with `--name-status` and parse them through `_changed_paths_from_name_status_z`.
4. Run the targeted unit test file or selected tests that cover this recovery helper.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`

Pass criteria: targeted tests pass, including the rename-source policy refresh regression. Broad validation is intentionally left to AWF/GitHub after this agent phase.
