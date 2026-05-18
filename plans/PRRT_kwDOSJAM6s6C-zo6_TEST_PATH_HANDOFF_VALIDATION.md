# PRRT_kwDOSJAM6s6C-zo6 Test Path Handoff Validation

Plan reference: `PRRT_kwDOSJAM6s6C-zo6_TEST_PATH_HANDOFF_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing named validation command handoff
  mixed with add/create/update test-path work remains agent-owned.
- Complete: Preserved existing acceptance for pure validation command handoff
  gaps that include test paths as command arguments.
- Complete: Updated the classifier to keep path-shaped `test/` and `tests/`
  command arguments eligible for AWF handoff unless nearby deterministic
  test-file work verbs are present.
- Complete: Ran focused validation for the changed behavior and ruff on the
  touched Python files.

## Evidence

- Changed `src/awf/runtime/planning.py`.
- Changed `tests/unit/runtime/test_planning.py`.
- Added this validation record and the paired plan file.
- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps -q`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`

## Gaps

None.
