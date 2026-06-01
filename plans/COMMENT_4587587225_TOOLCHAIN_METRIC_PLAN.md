# Comment 4587587225 Toolchain Metric Plan

## Problem Statement And Scope

The PR monitor maps `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` to the same
`pre_push_validation_failed` git-push metric outcome as retryable pre-push
validation failures. Since toolchain-missing is terminal and does not consume a
fix pass, operators need a distinct outcome label for dashboard triage.

Scope is limited to the PR monitor push outcome label and its focused unit
tests. No AWF/GitHub-owned broad validation will be run inside this agent phase.

## Requirements Checklist

- Keep retryable pre-push validation reasons mapped to
  `pre_push_validation_failed`.
- Map `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` to a dedicated
  `pre_push_validation_toolchain_missing` outcome.
- Preserve terminal-monitor behavior for toolchain-missing.
- Add or update focused regression coverage for the new outcome.
- Commit only the files changed for this review comment.

## Implementation Steps

1. Update the focused outcome test to expect a dedicated toolchain-missing
   label and run it to confirm the current behavior fails.
2. Change `_git_push_failure_outcome` so the toolchain-missing reason returns
   the dedicated outcome before retryable pre-push buckets.
3. Run targeted unit tests for the affected remote-ops behavior.
4. Record validation evidence in a matching validation document.
5. Stage the changed files and create a local conventional commit.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py -q`
  - Passes with the dedicated toolchain-missing outcome and unchanged terminal
    behavior.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
