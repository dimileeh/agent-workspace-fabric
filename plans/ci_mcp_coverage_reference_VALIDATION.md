# CI MCP Coverage Reference Validation

Plan reference: `plans/ci_mcp_coverage_reference_PLAN.md`

## Requirement Status

- Complete: Preserved the existing CI check behavior. The smoke assertion still
  requires every `MCP implemented` parity row with an MCP tool to have an
  explicit executable coverage reference.
- Complete: Reproduced the reported focused failure before implementation.
- Complete: Added an explicit executable contract/parity coverage reference for
  `Workspace create v2` in
  `tests/unit/contracts/test_registry_smoke.py`.
- Complete: Verified the focused smoke test passes after the fix.
- Complete: Verified the referenced create-v2 contract test passes.
- Complete: Created this validation document against the saved plan.

## Evidence

Files changed:

- `tests/unit/contracts/test_registry_smoke.py`
- `plans/ci_mcp_coverage_reference_PLAN.md`
- `plans/ci_mcp_coverage_reference_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q`
  - Before implementation: failed with missing `Workspace create v2` coverage reference.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference tests/unit/contracts/test_request_payload_alignment.py::test_mcp_create_v2_hydrates_canonical_request_model -q`
  - After implementation: passed, `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py -q`
  - After implementation: passed, `47 passed`.

## Remaining Gaps

None.
