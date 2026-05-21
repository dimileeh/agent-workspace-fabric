# Review 4496235802 Preserved-Active Recovery Validation

Plan reference:
`plans/REVIEW_4496235802_PRESERVED_ACTIVE_RECOVERY_PLAN.md`

## Requirement Status

- Complete: Failed open-PR lookup after grace no longer writes immediate
  operator-required recovery.
- Complete: Failed open-PR lookup now falls through to local worktree
  classification and clean committed work requests validate-only salvage.
- Complete: Operator-required recovery remains the terminal outcome for
  ambiguous or failed local classification after grace.
- Complete: No-work local classification still creates a replacement workspace
  instead of operator-required recovery.
- Complete: Preserved-active validation dispatch is skipped when the validation
  request was a no-op because a fresh execution claim is live.
- Complete: Existing idempotency and stale-claim guards remain in the salvage
  writers; the validation request helper now reports whether it actually wrote
  recovery state.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`

Protocol artifacts:

- `plans/REVIEW_4496235802_PRESERVED_ACTIVE_RECOVERY_PLAN.md`
- `plans/REVIEW_4496235802_PRESERVED_ACTIVE_RECOVERY_VALIDATION.md`

Tests and checks:

- Focused regressions were run before implementation and failed for the reported
  behaviors.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "pr_lookup_failure_validates_committed_work or pr_lookup_failure_with_no_local_work_replaces or validation_noop_for_fresh_claim"`:
  passed, 3 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker.py`:
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`:
  passed, 263 passed.

## Gaps

None.
