# Validation: PR287 OpenAPI Drift Fix

## Plan reference
- [plans/PR287_OPENAPI_DRIFT_PLAN.md](plans/PR287_OPENAPI_DRIFT_PLAN.md)

## Requirement status
1. Reproduce focused failure: **Complete**
   - Command: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_docs_drift.py::test_openapi_json_consistent_with_current_app -q`
   - Result: failed before fix (1 failed)
2. Regenerate authoritative OpenAPI: **Complete**
   - Command: `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
   - Result: wrote updated `openapi.json`
3. Apply minimal delta to checked-in artifact: **Complete**
   - File changed: `openapi.json` (added missing schema description for `LocalCapacitySourceResponse`)
4. Re-run focused repro: **Complete**
   - Command: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_docs_drift.py::test_openapi_json_consistent_with_current_app -q`
   - Result: passed
5. Validation file capture: **Complete**
   - This file records statuses and evidence.
