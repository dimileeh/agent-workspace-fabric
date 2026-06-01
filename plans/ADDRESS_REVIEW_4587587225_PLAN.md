# Address Review Comment 4587587225 Plan

## Problem Statement and Scope

PR review comment `issue:4587587225` flagged one robustness issue in PR
monitor handoff setup-failure handling:

- `_mark_failed_from_monitor_handoff_setup_failure` catches a failed
  high-level `_mark_failed` call, checks that `_session_factory` exists, then
  retries the same `_mark_failed` wrapper before reaching the useful direct DB
  fallback. Non-transient wrapper failures therefore produce duplicate
  exception logs and delay the fallback path.

Scope is limited to monitor handoff code and focused unit coverage for the
helper guard. No GitHub writes, branch switching, pushing, or broad AWF/CI
validation will be performed.

## Requirements Checklist

- Update the focused regression so a setup-failure direct fallback error proves
  only one high-level `_mark_failed` attempt occurs before direct persistence.
- Update `_mark_failed_from_monitor_handoff_setup_failure` to call
  `_persist_monitor_handoff_setup_failure_directly` immediately after a failed
  `_mark_failed` when `_session_factory` is available.
- Remove the redundant
  `executor.monitor_handoff_setup_failure_final_mark_failed_failed` log path.
- Preserve existing setup-failure reason codes, details, and successful
  fallback persistence behavior.
- Run focused tests for the changed helper/release handoff behavior only; note
  that full AWF/GitHub validation is handled after agent completion.

## Implementation Steps

1. Update the existing direct-fallback regression in the monitor handoff setup
   test file to expect one `_mark_failed` call and no duplicate final-wrapper
   exception log.
2. Run the narrow regression test and confirm it fails on the current code.
3. Simplify the helper fallback flow so the direct persistence path is reached
   after the first wrapper failure.
4. Run targeted tests covering the updated helper behavior.
5. Write validation evidence in
   `plans/ADDRESS_REVIEW_4587587225_VALIDATION.md`.
6. Stage only touched files and commit locally with a conventional commit
   referencing comment `4587587225`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -k "handoff_setup or mark_failed_from_monitor_handoff_setup_failure" -q`
  - Passes after implementation.
- Full AWF/GitHub validation is intentionally not run in this agent phase.
