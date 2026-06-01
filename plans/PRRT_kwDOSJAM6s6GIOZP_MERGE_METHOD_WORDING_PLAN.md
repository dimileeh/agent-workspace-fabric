# PRRT_kwDOSJAM6s6GIOZP Merge Method Wording Plan

## Problem Statement And Scope

GitHub merge attempts can reject a merge method with current wording such as
`Merge method squash merging is not allowed`, `Merge method merge commit is not allowed`,
or `Merge method rebase is not allowed`. The PR monitor merge-method classifier currently
recognizes the older phrasing only, so these permanent policy rejections may be handled as
generic merge blockers instead of retrying the next effective merge method or recording the
merge-method blocker.

Scope is limited to `src/awf/runtime/pr_monitor_runner/merge_loop.py` and the existing
merge-method regression tests.

## Requirements Checklist

- Recognize GitHub's current squash merge-method rejection wording.
- Recognize GitHub's current merge-commit merge-method rejection wording.
- Recognize GitHub's current rebase merge-method rejection wording.
- Preserve existing older wording behavior and non-method generic blocker behavior.
- Verify with focused unit tests only; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add failing regression assertions for the current GitHub wording in
   `tests/unit/runtime/test_pr_monitor_merge_methods.py`.
2. Run the focused classifier test to confirm the regression fails.
3. Update `_merge_method_rejection_method` to classify the current wording.
4. Run the focused merge-method unit tests affected by the change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py::test_merge_method_rejection_classifier_is_specific -q`
  passes after implementation and fails before implementation for the new assertions.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passes after implementation.
