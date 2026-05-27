# Plan: Fix OpenAPI drift check for PR #287

## Problem scope
`tests/unit/api/test_docs_drift.py::test_openapi_json_consistent_with_current_app` fails in CI because the checked-in `openapi.json` is not synchronized with the current FastAPI schema.

## Requirements checklist
- [ ] Reproduce the failure with the focused repro command.
- [ ] Regenerate the OpenAPI spec used by the application.
- [ ] Identify and apply the minimal delta to `openapi.json`.
- [ ] Re-run the focused repro check and confirm it passes.
- [ ] Create a validation file documenting pass/fail status and evidence.

## Implementation steps
1. Run the focused pytest case provided in CI context.
2. Run OpenAPI generation under UV-managed deps to get the authoritative spec.
3. Update `openapi.json` to match current generated output.
4. Re-run the focused check.

## Verification commands
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_docs_drift.py::test_openapi_json_consistent_with_current_app -q`
- `python scripts/generate_openapi.py` (for local generation under UV)
