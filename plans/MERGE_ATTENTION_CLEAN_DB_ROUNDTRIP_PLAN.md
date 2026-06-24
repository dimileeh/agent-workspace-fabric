# Merge Attention CLEAN DB Round-Trip Plan

## Problem Statement and Scope

Greptile reported that GitHub `CLEAN` handling for an active merge-block attention
marker opens a fresh database session to decide whether the marker originated from
a merge rejection. The current branch already stores structured merge-rejection
origin in `MonitorState`, so queue-wait `CLEAN` preservation should use that
in-memory state before falling back to persisted legacy reason text.

Scope is limited to `src/awf/runtime/pr_monitor_runner/merge_attention.py` and
focused unit coverage for this behavior.

## Requirements Checklist

- Avoid a database read on GitHub `CLEAN` queue-wait polls when the in-memory
  merge-block marker has structured merge-rejection origin.
- Preserve the existing legacy fallback for rows whose persisted state predates
  structured origin metadata.
- Keep existing CLEAN behavior: merge-rejection origin preserves, ordinary
  non-rejection markers clear.
- Add focused regression coverage for the no-session structured-origin path.
- Run only targeted tests/checks; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Change the merge-rejection-origin helper to accept `MonitorState` and return
   immediately when the structured origin is present in memory.
2. Keep the existing DB-backed compatibility read only when state lacks structured
   origin metadata.
3. Update call sites in merge-attention helpers.
4. Add a unit test that monkeypatches the runner session factory to fail and
   proves a structured merge-rejection marker under GitHub `CLEAN` preserves
   without opening a session.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py -q`
  - Passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention.py`
  - Passes.

Full AWF/GitHub validation is intentionally not run inside this agent phase.
