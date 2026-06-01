# Address Review Comment 4590903660 Notification Wording Plan

## Problem Statement And Scope

Greptile flagged that the merge-method blocker notification can be misleading
when GitHub rejects the only effective method with the generic "could not be
merged with this method" wording. In that case the attempted method and
effective allowed method are intentionally identical, but the existing message
says the selected method is "not allowed", implying a policy mismatch instead
of a runtime refusal after all allowed methods were exhausted.

Scope is limited to the human-facing merge-method mismatch message and a
focused regression assertion for the exhausted generic-method path.

## Requirements Checklist

- Change the notification text so exhausted method attempts do not imply that
  an allowed method was disallowed by policy.
- Preserve existing fields: `attempted`, `effective_allowed`, optional GitHub
  detail, and the 2000-character cap.
- Add focused test coverage for the generic GitHub rejection with no remaining
  alternative.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Update `_merge_method_mismatch_message` in `merge_loop.py` to say no merge
   method succeeded for the base branch.
2. Tighten the existing `test_unclassified_last_method_rejection_records_method_blocker`
   assertions to verify the new message and the identical attempted/allowed
   fields are not framed as a policy mismatch.
3. Run the targeted merge-method test file and focused ruff check for touched
   files.
4. Record evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passes.
- Full AWF/GitHub validation is not run locally per workspace contract.
