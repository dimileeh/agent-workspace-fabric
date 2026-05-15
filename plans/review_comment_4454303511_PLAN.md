# Review Comment 4454303511 Plan

## Problem Statement and Scope

Greptile's review-level comment on PR #248 identifies two follow-up risks in
the setup dependency network retry work:

- the `http_5xx` transient classifier may match broad bare 5xx text in
  non-HTTP output and cause unnecessary bounded retries;
- the executor monitor-recovery setup-exhaustion path should have regression
  coverage if it is not already covered.

This plan is limited to addressing that review comment. It must not change git
branch state, push, weaken existing regression tests, or alter unrelated retry
semantics.

## Requirements Checklist

- Verify current recovery-path coverage for monitor-dispatched setup dependency
  retry exhaustion.
- Add or update a failing regression test for any real gap before changing
  implementation.
- Tighten `http_5xx` classification so bare numeric 5xx text is only treated as
  transient when it is clearly HTTP/status-code context, while preserving known
  dependency fetch 5xx behavior.
- Keep setup retry metadata, event emission, and failure reason behavior
  unchanged except for the narrowed false-positive classification.
- Commit only the files changed for this comment using a conventional commit
  message.

## Implementation Steps

1. Inspect the current classifier, tests, and recovery-path coverage.
2. If the recovery-path test already exists, treat that portion of the comment
   as stale and avoid duplicating it.
3. Add a focused runtime classifier regression test for a dependency setup
   command whose output contains bare non-HTTP 5xx text.
4. Update the transient `http_5xx` matching logic to require explicit HTTP
   status context for bare 5xx values.
5. Run the narrow runtime and executor tests that prove the review comment is
   handled; expand only if failures point outside the touched surface.
6. Write validation results in
   `plans/review_comment_4454303511_VALIDATION.md`.
7. Stage the changed files and commit locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q`
  passes, or an equivalent narrow selector passes if full-file runtime is
  excessive.
- `git status --short` shows only intentional files before commit and a clean
  worktree after commit.
