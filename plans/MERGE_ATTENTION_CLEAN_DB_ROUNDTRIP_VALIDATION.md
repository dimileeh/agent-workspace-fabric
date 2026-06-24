# Merge Attention CLEAN DB Round-Trip Validation

Plan reference: `plans/MERGE_ATTENTION_CLEAN_DB_ROUNDTRIP_PLAN.md`

## Requirement Status

- Avoid a database read on GitHub `CLEAN` queue-wait polls when structured
  merge-rejection origin is in memory: Complete.
  - `_merge_block_attention_originated_from_merge_rejection` now returns from
    `MonitorState.merge_block_attention_originated_from_merge_rejection()` before
    opening a session.
  - Added `test_github_clean_structured_merge_rejection_preserve_uses_state_not_db`,
    which fails if the structured-origin preserve path touches `session_factory`.

- Preserve legacy fallback for rows predating structured origin metadata:
  Complete.
  - The existing DB-backed workspace read and human-reason compatibility check
    remains in place when the structured origin key is absent from state.

- Keep existing CLEAN behavior for rejection vs non-rejection markers: Complete.
  - Existing queue-wait regression tests still cover GitHub `CLEAN` preserve for
    rejection-origin attention and clear for ordinary non-rejection attention.

- Add focused regression coverage: Complete.
  - New regression coverage added in
    `tests/unit/runtime/test_pr_monitor_merge_attention.py`.

- Run only targeted validation: Complete.
  - Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
    validation, provenance, and merge gating after completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `tests/unit/runtime/test_pr_monitor_merge_attention.py`
- `plans/MERGE_ATTENTION_CLEAN_DB_ROUNDTRIP_PLAN.md`
- `plans/MERGE_ATTENTION_CLEAN_DB_ROUNDTRIP_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Passed: `23 passed in 30.94s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py -q`
  - Passed: `10 passed in 0.79s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passed: `All checks passed!`

No remaining planned gaps.
