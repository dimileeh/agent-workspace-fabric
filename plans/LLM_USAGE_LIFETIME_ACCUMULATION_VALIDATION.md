# Lifetime LLM Usage Accumulation Validation

Plan reference: `plans/LLM_USAGE_LIFETIME_ACCUMULATION_PLAN.md`

## Requirement Status

- Public API and console response shape unchanged: Complete.
- Fresh per-run ccusage baselines preserved: Complete.
- Token and cost metrics accumulate across agent runs for one workspace id:
  Complete.
- Prior accumulated totals are preserved when later ccusage data is missing or
  unavailable: Complete.
- Snapshot metadata records current run delta and accumulated-at-start values:
  Complete.
- Old snapshot JSON files remain readable: Complete.
- Focused regression coverage added for store helpers, collector behavior, and
  observability handling: Complete.

## Evidence

Changed files:

- `src/awf/service/usage_store.py`
- `src/awf/service/usage_collection.py`
- `tests/unit/service/test_usage_store.py`
- `tests/unit/service/test_usage_collection.py`
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_002.py`

Validation commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_002.py -q
```

Result: `157 passed in 1.88s`

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_002.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py
```

Result: `Success: no issues found in 3 source files`

## Notes

Existing snapshots cannot reconstruct usage from earlier runs that were already
overwritten before this fix. After deployment, the current snapshot becomes the
accumulation base and later agent runs add to it.
