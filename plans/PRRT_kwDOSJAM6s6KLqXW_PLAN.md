# PRRT_kwDOSJAM6s6KLqXW Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6KLqXW` reports that unrecoverable git repair
results are persisted as non-terminal monitor push failures. The scope is to
verify and, if needed, make `_GitPushResult.terminal_monitor_failure` treat
deterministic unrecoverable HEAD-object and mirror-hooks repair failures as
terminal so the monitor fails the workspace with the preserved reason instead
of retrying until the outer loop cap.

## Requirements Checklist

- Confirm the reported reason codes are produced by existing repair paths.
- Add a focused regression test for terminal classification of the reported
  unrecoverable repair reason codes.
- Update only the minimal production classification needed for those reason
  codes.
- Run targeted validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Inspect `ci_ops.py`, `pre_push_validation.py`, `constants.py`, and
   `remote_ops.py` around the reported paths and terminal classification.
2. Add a regression in `tests/unit/runtime/test_pr_monitor_remote_ops.py` that
   fails while the unrecoverable reason codes are non-terminal.
3. Add the missing reason codes to `_GitPushResult.terminal_monitor_failure`.
4. Re-run the targeted regression test file or specific test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
  passes.
- Record that broad AWF/GitHub validation is intentionally not run inside this
  agent phase per the workspace contract.
