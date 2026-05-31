# Address PRRT_kwDOSJAM6s6F57FM Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6F57FM` reports that the merge pre-check
operator-hint re-dispatch drops `remote_push_url`. In adopted or fork PR
workspaces this can make the operator-hint repair push to `origin` instead of
the PR head push URL.

Scope is limited to preserving `remote_push_url` across the merge-loop
re-dispatch and adding a focused regression test. No branch changes, pushes, or
broad AWF/GitHub validation will be performed.

## Requirements Checklist

- Preserve the current `remote_push_url` when `_execute()` delegates merge
  handling into the merge-loop helper.
- Preserve `remote_push_url` when merge-loop pre-merge rechecks dispatch a
  fresh non-merge action, including persisted operator hints.
- Add a regression test proving persisted operator-hint repair receives the
  fork/adopted push URL discovered by the outer monitor loop.
- Run only focused validation for the changed behavior.

## Implementation Steps

1. Add a failing unit test in the operator-hint monitor tests that passes a
   non-`origin` `remote_push_url` into a `Merge` action with a persisted
   operator hint and asserts `_run_operator_hint_cycle()` receives that URL.
2. Thread `remote_push_url` through `_merge_loop.handle_merge_action()` from
   `_execute()`.
3. Forward `remote_push_url` through merge-loop recursive `_execute()` calls.
4. Run the targeted regression test and focused lint on the touched files.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F57FM_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::test_merge_recheck_preserves_remote_push_url_for_persisted_operator_hint -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`

Pass criteria: the targeted regression test fails before the fix when
practical, passes after implementation, and the focused lint check passes. Full
AWF/GitHub validation remains managed after agent completion.
