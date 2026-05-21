# Review 4496235802 Validation

Plan reference: `plans/REVIEW_4496235802_PLAN.md`

## Requirement Status

- Complete: Added a regression test for the idempotent replacement re-entry path
  where the existing replacement workspace has no task attempt.
- Complete: Added a regression test showing `SALVAGE_BLOCKED` writes a new event
  when the latest blocked reason changes, while preserving exact-repeat
  deduplication.
- Complete: Updated `src/awf/control/worker.py` so missing replacement attempts
  emit structured warning evidence and blocked salvage dedupe compares the
  latest blocked reason.
- Complete: Review-specific tests, ruff, and mypy passed.
- Partial: The full `tests/unit/control/test_worker.py` command was attempted
  but still fails on this branch for pre-existing failures that reproduce in a
  clean `HEAD` snapshot outside this patch.

## Evidence

- Changed `src/awf/control/worker.py`.
- Changed `tests/unit/control/test_worker.py`.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'salvage_blocked_records_changed_reason or replacement_missing_existing_attempt_logs_warning'`: `2 passed, 274 deselected`.
- Ran `uv run --python 3.12 --extra dev ruff check src/awf tests`: passed.
- Ran `uv run --python 3.12 --extra dev mypy src/awf`: passed.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`: `10 failed, 266 passed`.
- Baseline check from a clean `HEAD` archive reproduced representative full-file
  failures:
  - `preserved_active_without_usable_work_creates_one_replacement_with_lineage`
    failed with no replacement workspace created.
  - `stale_active_scan_closed_connection_does_not_terminal_fail_workspace`
    failed because runtime inspection was called.
  - `preserved_active_validation_salvage_without_executor_blocks_stale_cleanup`
    timed out after 120 seconds.

## Iteration 1

The only validation gap is the full worker suite, and the representative
failures reproduce without this patch. No additional iteration is required for
review comment `issue:4496235802`; fixing those broader branch failures would be
separate work.
