# Bound Satisfied Conformance Fallback Plan

## Problem Statement And Scope

The fallback served-artifact deposit in `src/awf/control/executor/planning_conformance.py` serializes an in-memory satisfied conformance report directly to `artifacts/{workspace_id}/conformance.json`. The normal planning artifact deposit rejects content larger than `MAX_ARTIFACT_CONTENT_BYTES`; this fallback does not, so a very large stdout-derived report can create an oversized served artifact.

Scope is limited to bounding this fallback report write and adding a focused regression test. No GitHub writes, branch changes, push, or broad validation will be performed.

## Requirements Checklist

- [ ] Verify the fallback rejects serialized conformance report content larger than the shared artifact content cap before writing to the served artifact directory.
- [ ] Preserve existing best-effort behavior: deposit failures or rejections must not fail post-validation conformance handling.
- [ ] Keep normal fallback deposits unchanged for reports within the cap, including best-effort plan copy after successful report deposit.
- [ ] Add a focused regression test for the oversized fallback report path.
- [ ] Run only targeted tests/checks for the changed behavior and document that AWF owns broad validation after agent completion.

## Implementation Steps

1. Add an oversized-report regression test near existing satisfied-conformance fallback deposit tests.
2. Run the new targeted test and confirm it fails against current code.
3. Import the shared artifact cap and reject oversized serialized fallback content before writing the temp report.
4. Run the focused regression tests for the affected fallback deposit behavior.
5. Create validation notes with evidence and commit the scoped changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q -k 'deposit_satisfied_conformance_report'`
  - Pass criteria: all focused fallback deposit tests pass.

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns broad validation, provenance, logs, and merge gating after completion.
