# PRRT_KxJt3 Runtime Unstage Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6KxJt3` reports that missing-HEAD filesystem
recovery excludes AWF agent runtime paths with `git rm --cached`, which stages
deletions instead of only unstaging those paths.

Scope is limited to the missing-HEAD recovery path in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused regression
coverage for that command sequence.

## Requirements Checklist

- Verify the review claim against current code.
- Add a regression test showing runtime-root paths are unstaged with
  literal pathspecs rather than removed from the index.
- Replace `git rm --cached` with a non-destructive index unstage command.
- Run targeted tests only; AWF/GitHub own broad validation after this agent
  finishes.
- Commit the focused fix locally.

## Implementation Steps

1. Add a focused unit test next to existing
   `_recover_missing_head_object_from_filesystem` tests.
2. Confirm the new test fails against the current `git rm --cached` behavior.
3. Update the recovery command to use
   `git --literal-pathspecs reset -q HEAD -- <excluded paths>`.
4. Re-run the targeted unit test.
5. Save validation evidence in `plans/PRRT_KxJt3_RUNTIME_UNSTAGE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k recover_missing_head_object`
  - Passes after implementation.
  - Before implementation, the new regression fails because the reset command is
    absent.

Full AWF/GitHub validation is intentionally not run in-agent per workspace
contract.
