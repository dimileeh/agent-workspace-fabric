# Merge Attention DB Origin Plan

## Problem Statement And Scope

An unresolved PR review thread reports that a GitHub `CLEAN` queue/reviewer/grace wait can preserve merge-block attention because the merge-rejection origin exists only in the workspace row, while the in-memory `MonitorState` still lacks `__awf_merge_block_attention_origin__`. The outer monitor loop later persists the full in-memory state, which can overwrite `monitor_threads_addressed` and erase the DB-derived origin.

Scope is limited to `src/awf/runtime/pr_monitor_runner/merge_attention.py` and focused regression coverage under `tests/`.

## Requirements Checklist

- Reproduce the reported state-loss path with a focused regression test.
- Preserve existing behavior for explicit in-memory non-rejection origins.
- Copy DB-derived merge-rejection origin into in-memory state before returning from the queue-wait preserve branch.
- Avoid broad refactors or unrelated validation.
- Commit the local fix with a conventional commit message tied to the thread id.

## Implementation Steps

1. Add a regression test that seeds a persisted merge-rejection origin, calls `_clear_or_preserve_merge_attention_for_queue_wait()`, then calls `_persist_state()` and verifies the origin remains durable.
2. Update merge-attention origin detection/preserve logic so DB-derived merge-rejection origin is materialized into `MonitorState`.
3. Run the focused regression test file or specific tests only.
4. Create `plans/MERGE_ATTENTION_DB_ORIGIN_VALIDATION.md` with requirement-by-requirement evidence.
5. Stage only changed files and commit locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q`

Pass criteria: the new regression and existing merge-attention persistence tests pass. Full AWF/GitHub validation is managed after agent completion and is intentionally not run here.
