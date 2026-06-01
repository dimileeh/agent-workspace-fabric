# Address Review Comment 4399920085 Plan

## Problem statement and scope

CodeRabbit reported that the merge-attempt loop in
`src/awf/runtime/pr_monitor_runner/merge_loop.py` is deeply nested and suggested
extracting the inner merge attempt try/except/else into a helper with an
explicit result outcome. The current behavior appears valid, so the scope is a
small maintainability refactor only.

## Requirements checklist

- Verify the review comment against current `merge_loop.py`.
- Extract per-method merge attempt behavior into a helper without changing
  merge behavior.
- Preserve monitor operation lifecycle calls, merge audit events, method
  fallback decisions, merge-method blocker persistence, and transient blocker
  propagation.
- Keep changes scoped to the merge loop and plan/validation records.
- Run focused validation for the changed merge-method behavior only; AWF owns
  broad validation after agent completion.

## Implementation steps

1. Add a small internal result enum/data object for merge attempt outcomes.
2. Move the per-method `merge_pr` try/except/else logic into an internal helper.
3. Replace the inner loop body with outcome handling for success, retry next
   method, merge-method blocker, or transient blocker.
4. Run focused tests covering merge method fallback and blocker behavior.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  must pass.
- Optional focused syntax/type sanity for the touched file may be run if needed.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
