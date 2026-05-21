# Review 4496235802 Preserved-Active Recovery Plan

## Problem Statement And Scope

The PR review identified preserved-active restart recovery cases where worker
restart salvage can stop too early or do unnecessary dispatch work.

Scope is limited to `src/awf/control/worker.py` and focused unit regressions in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- When open-PR lookup fails after the preservation grace period expires, do not
  immediately write operator-required recovery.
- Fall through to preserved-active worktree classification after a failed
  open-PR lookup; recover clean committed work through validate-only salvage.
- Preserve operator-required behavior when local worktree classification is
  ambiguous or failed after grace.
- Preserve replacement behavior when local classification shows no recoverable
  work.
- Do not dispatch preserved-active validation when the validation request was a
  no-op because a fresh execution claim is still live.
- Keep existing stale-claim, idempotency, and duplicate-monitor protections.

## Implementation Steps

1. Update the failed open-PR lookup branch in
   `_recover_preserved_active_execution` so only pre-grace failures block and
   expired failures continue into local worktree classification.
2. Thread failed lookup payload into later salvage outcomes where useful, so
   validate/operator event payloads retain lookup provenance.
3. Change `_request_preserved_active_validation` to report whether it actually
   requested validation and dispatch only when that is true.
4. Add or update unit tests for failed lookup with committed work, failed
   lookup with no local work, and fresh-claim no-op dispatch prevention.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes, or any failure is unrelated and documented in validation.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker.py`
  passes.
