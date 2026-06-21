# Operator Hint HEAD Object Threading Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6KxAJH` reports that operator-hint CLI repair
does not fully preserve missing-HEAD handling when `_commit_dirty_worktree`
raises `_MonitorHeadObjectMissingError`. The current handler catches that
exception, but the CLI verdict invocation does not pass the captured
`operation_start_head`, so downstream recovery may use a later or fallback SHA.

Scope is limited to the operator-hint repair path and its focused unit tests.

## Requirements Checklist

- Preserve the existing `_MonitorHeadObjectMissingError` structured
  `_GitPushResult` behavior for operator hints.
- Pass the captured `operation_start_head` into `_invoke_cli_for_verdict_result`
  for operator-hint CLI repairs.
- Pin the threading behavior with a focused regression test.
- Do not run broad AWF/GitHub-owned validation; use targeted tests only.

## Implementation Steps

1. Update the existing missing-HEAD operator-hint unit test to assert that the
   CLI verdict call receives the captured operation start SHA.
2. Confirm the targeted test fails before the implementation change when
   practical.
3. Pass `operation_start_head=operation_start_head` in the operator-hint CLI
   verdict invocation.
4. Re-run the focused test module or narrowed test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "head_object_missing or uses_captured_operation_start_head"`

Pass criteria: the focused operator-hint tests pass, and no broad validation is
run locally because AWF/GitHub owns full validation after agent completion.
