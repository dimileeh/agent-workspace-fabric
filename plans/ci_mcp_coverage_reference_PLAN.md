# CI MCP Coverage Reference Plan

## Problem Statement and Scope

PR #247 is failing the focused contract smoke test
`tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference`.
The parity matrix marks `Workspace create v2` as `MCP implemented`, but the
smoke-test coverage-reference map does not list an executable contract/parity
test for that capability.

Scope is limited to restoring the missing explicit coverage reference without
weakening the registry smoke check or changing runtime behavior.

## Requirements Checklist

- Preserve the existing CI check behavior; do not skip, disable, or loosen the
  assertion.
- Reproduce the reported focused failure before implementation when practical.
- Add an explicit executable contract/parity coverage reference for
  `Workspace create v2`.
- Verify the focused smoke test passes after the fix.
- Create a validation document that checks implementation against this plan.
- Commit the fix locally on the current AWF branch.

## Implementation Steps

1. Run the reported focused pytest node to confirm the local failure.
2. Identify an existing executable test that exercises
   `awf_create_workspace_v2` request/response parity or contract alignment.
3. Add `Workspace create v2` to `IMPLEMENTED_PARITY_COVERAGE_REFERENCES` with
   that test node ID.
4. Re-run the focused pytest node and any narrow nearby contract test needed to
   validate the reference resolves.
5. Write `plans/ci_mcp_coverage_reference_VALIDATION.md` with requirement
   status and evidence.
6. Commit the plan, validation, and code fix locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q`
  passes.
- If the selected reference points into a nearby contract test file, run that
  referenced test node and require it to pass.
