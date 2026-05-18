# PRRT_kwDOSJAM6s6C-9wz Test Path Work Phrases Plan

## Problem Statement And Scope

The conformance validation handoff classifier treats named validation-command
gaps containing `tests/...` paths as AWF-owned unless a narrow verb-to-path
context proves the test path is still implementation work. Review feedback
reports that phrases such as `add assertions in tests/unit/...` are currently
misclassified because the helper only recognizes bare modifiers before the test
path.

Scope is limited to `src/awf/runtime/planning.py` and focused unit coverage in
`tests/unit/runtime/test_planning.py`.

## Requirements Checklist

- Add a regression test proving a mixed validation handoff plus test-file work
  phrase like `add assertions in tests/unit/...` remains agent-owned.
- Preserve existing acceptance of validation-command handoffs that list test
  paths as command arguments.
- Update the test-path work context classifier to recognize common test-file
  work nouns and prepositions between the work verb and path.
- Run the narrow unit test selection that covers the changed behavior.

## Implementation Steps

1. Add the failing regression case to the mixed named-command test path work
   parametrization.
2. Run that narrow test and confirm the new case fails before implementation.
3. Extend `_has_test_path_work_context` with bounded work-object phrases such as
   `assertions in`, while keeping command path handoffs accepted.
4. Re-run the narrow unit test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q -k "named_command_handoff_with_paths or mixed_named_command_test_path_work_gaps"`
  passes.
