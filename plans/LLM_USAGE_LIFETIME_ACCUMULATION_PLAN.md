# Lifetime LLM Usage Accumulation Plan

## Problem Statement

AWF currently reports `llm_usage` from the latest agent invocation rather than
the full workspace lifetime. `CcusageCollector` captures a fresh baseline for
each `AgentAdapter.run` and writes a latest-wins `snapshot.json`; when PR monitor
turns run after PR creation, those later runs overwrite earlier usage totals.

## Requirements Checklist

- Keep the public API and console response shape unchanged.
- Preserve fresh per-run ccusage baselines so prior transcripts are not
  double-counted.
- Accumulate token and cost metrics across all agent runs for the same
  workspace id.
- Preserve prior accumulated totals when a later run has missing/unavailable
  ccusage data.
- Add snapshot metadata that exposes per-run delta and accumulated-at-start
  values for diagnostics.
- Keep old snapshot JSON files readable.
- Add focused regression coverage for store helpers, collector behavior, and
  observability handling.

## Implementation Steps

1. Add usage-store helpers for converting snapshots to `NormalizedUsage`,
   adding prior accumulated usage to a current run delta, and serializing
   optional metadata.
2. Make `CcusageCollector.start` load the current snapshot before the new run,
   store its metrics as the accumulated-at-start value, and write lifetime
   totals instead of raw per-run deltas.
3. Keep baseline capture fresh on every run and continue to seed an immediate
   live snapshot, but seed it with the prior lifetime total rather than zero.
4. When ccusage is unavailable for the current run, preserve prior accumulated
   metrics when present and record the reason code in the snapshot.
5. Update tests that previously asserted per-run reset semantics to assert
   lifetime accumulation instead.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_002.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py
```

Pass criteria: targeted tests pass, lint passes, and mypy reports no errors on
the touched service modules.
