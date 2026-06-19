# Bound Satisfied Conformance Fallback Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6KxUlq_PLAN.md`

## Requirement Status

- Complete: The fallback rejects serialized conformance report content larger than
  `MAX_ARTIFACT_CONTENT_BYTES` before writing to the served artifact directory.
- Complete: Best-effort behavior is preserved; oversized fallback deposits log a
  rejection and return without failing post-validation conformance handling.
- Complete: Normal fallback deposits within the cap are unchanged; existing
  fallback deposit tests still pass.
- Complete: Added a focused regression test for the oversized stdout-derived
  fallback report path.
- Complete: Ran only targeted tests/checks for the changed behavior. Full
  AWF/GitHub validation is managed by AWF after agent completion.

## Evidence

- Changed `src/awf/control/executor/planning_conformance.py` to measure the
  UTF-8 serialized fallback report and reject oversized content before the
  temporary artifact write.
- Added
  `test_deposit_satisfied_conformance_report_rejects_oversized_report` in
  `tests/unit/control/test_planning_ops_branch_edges.py`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q -k 'rejects_oversized_report'`
  failed because `conformance.json` was written.
- Verified after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q -k 'rejects_oversized_report'`
  passed.
- Verified nearby fallback deposit behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q -k 'deposit_satisfied_conformance_report'`
  passed.
- Focused lint check:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_conformance.py tests/unit/control/test_planning_ops_branch_edges.py`
  passed.

## Gaps

None.
