# PRRT_kwDOSJAM6s6C-S1E Validation Handoff Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6C-S1E_VALIDATION_HANDOFF_PLAN.md`

## Requirement Status

- Complete: Named validation command handoff gaps remain accepted when they only
  ask AWF to run missing validation evidence.
- Complete: Mixed gaps that mention named validation commands and deterministic
  API/endpoint implementation work remain agent-owned.
- Complete: Existing deterministic filters for migration, schema,
  documentation, code, and test work still run before the classifier accepts a
  validation handoff.
- Complete: A regression test was added before implementation and failed against
  the previous classifier.

## Evidence

Files changed:

- `src/awf/runtime/planning.py`
- `tests/unit/runtime/test_planning.py`
- `plans/PRRT_kwDOSJAM6s6C-S1E_VALIDATION_HANDOFF_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6C-S1E_VALIDATION_HANDOFF_VALIDATION.md`

Commands run:

- Failed before implementation: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_handoff_gaps -q`
- Passed after implementation: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_handoff_gaps -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`

## Remaining Gaps

None.
