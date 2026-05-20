# Review Comment 4327734407 Validation

Plan reference: `plans/REVIEW_COMMENT_4327734407_PLAN.md`

## Requirement Status

- Complete: Branch PR resolver failures now record operator-required recovery before local worktree salvage, preserving the lookup failure payload and avoiding validation/replacement side effects when AWF cannot prove there is no open PR.
- Complete: Preserved-active salvage mutation helpers recheck `_execution_claim_is_stale(...)` after `get_for_update(...)` and return without side effects when another worker/operator has refreshed the execution claim.
- Complete: Already-addressed inline review items were left intact: restart recovery claim statuses include `running`, `validating`, and `pushing`; lookup failure payload assertions remain positive; replacement idempotency remains exact.
- Complete: The concurrent salvage-not-possible regression no longer uses a `0.2s` timeout as the serialization proof; it uses explicit events for second lock attempt, first recording, and second selection.
- Complete: Focused regressions were added/updated for resolver failure, fresh-claim post-lock rechecks, and deterministic concurrent salvage recording.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_COMMENT_4327734407_PLAN.md`
- `plans/REVIEW_COMMENT_4327734407_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'pr_lookup_failure or salvage_writers_recheck_fresh_execution_claim or salvage_not_possible_recording_serializes_concurrent_events'`
  - Result: passed, `9 passed, 202 deselected`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result: passed
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Result: failed, `11 failed, 200 passed`

## Remaining Gap

The full `tests/unit/control/test_worker.py` run still has 11 failures in pre-existing preservation-subphase expectations that assert `runtime_preserved_after_restart` while the current branch records `runtime_preserved_salvage_blocked` after preservation for missing-lineage live-runtime cases. The focused review-comment behavior is covered and passing; changing those broader expectations is outside this review comment fix.
