# Address Review Comment 4590903660 Plan

## Problem Statement And Scope

Greptile left a review-level comment on PR #353 with two focused issues in the
PR monitor merge-method path:

- `_execute_merge` in `tests/unit/runtime/test_pr_monitor_merge_methods.py`
  omits `remote_push_url`, so the merge-method `NotifyHuman` escalation path is
  not exercised with the same argument shape used by the runner.
- `_merge_error_supports_method_alternative` treats GitHub's generic
  "could not be merged with this method" text as method-related. Existing
  regression tests intentionally cover that behavior, so the fix should clarify
  the intent without weakening the blocker lifecycle.

Scope is limited to the focused merge-method test helper, a clarifying comment
in `merge_loop.py`, and this plan/validation pair. No protected workflow,
quality-gate, or broad validation configuration files will be edited.

## Requirements Checklist

- Pass a concrete `remote_push_url` through the merge-method test helper.
- Preserve existing merge-method rejection behavior and regression assertions.
- Clarify why the generic GitHub method text is classified as method-related.
- Run focused validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Update `_execute_merge` to pass
   `remote_push_url=f"git@github.com:{_TEST_REPO.slug()}.git"`.
2. Add a concise comment beside the generic method-text classifier explaining
   why it remains method-related despite GitHub's ambiguous wording.
3. Run targeted tests covering merge-method monitor behavior and focused lint
   for the touched files.
4. Record validation evidence in `ADDRESS_REVIEW_4590903660_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passes.
- Full AWF/GitHub validation is not run locally per workspace contract.
