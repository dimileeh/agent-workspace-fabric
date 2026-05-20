# PR #272 Git Classification Failure Retry Plan

## Problem Statement and Scope

Greptile's review comment for PR #272 reports that preserved-active worktree
classification treats git command failures as semantic ambiguity. That writes a
terminal operator-required salvage event and prevents later scans from retrying
after transient problems such as a stale `.git/index.lock` or a git timeout.

Scope is limited to preserved-active recovery in
`src/awf/control/worker.py`, focused unit coverage in
`tests/unit/control/test_worker.py`, and this plan/validation pair.

## Requirements Checklist

- Classify infrastructure git command failures as retryable
  `state="failed"` results instead of semantic `state="ambiguous"` results.
- Map retryable worktree-classification failures to
  `workspace.active_execution_salvage_blocked` during the preservation grace
  window, preserving retry on later scans.
- Preserve terminal `OPERATOR_REQUIRED` behavior for true semantic
  ambiguities such as detached head, branch mismatch, dirty worktree, missing
  branch metadata, missing base commit, and invalid ahead counts.
- Preserve expired-grace behavior by escalating persistent classification
  failures to operator-required recovery after grace expiry.
- Add a regression test proving a transient git status failure records blocked
  salvage first and then retries into validate-only salvage without writing
  operator-required.

## Implementation Steps

1. Add a focused unit test that makes the first preserved-active `git status`
   classification fail, verifies a blocked salvage event, then lets the next
   scan classify the committed worktree and request validation.
2. Update `_classify_preserved_active_worktree` so `branch_unavailable`,
   `head_unavailable`, `status_unavailable`, and
   `ahead_count_unavailable` return `state="failed"`.
3. Update `_recover_preserved_active_execution` to translate
   `classification.state == "failed"` into salvage-blocked during grace and
   operator-required after grace expiry.
4. Run focused worker tests plus scoped lint/type checks.
5. Record validation results in
   `plans/PR272_GIT_CLASSIFICATION_FAILURE_RETRY_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "git_status_failure_retries_during_grace or ambiguous_dirty_worktree or missing_branch_name"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passes, or unrelated pre-existing mypy blockers are documented.
