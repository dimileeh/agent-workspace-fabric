# Review 4585090228 Launch Cleanup Guard Plan

## Problem Statement And Scope

Address PR review comment `issue:4585090228` about
`src/awf/node/provisioner.py`: `_launch_lost_to_terminal_cleanup()` can raise
inside the `ComposeOperationError` and catch-all exception handlers, which can
mask the original launch failure and prevent `_mark_failed()` from finalizing
the workspace.

The scope is limited to the provisioner failure path and focused regression
coverage. No branch changes, push, broad AWF/GitHub validation, full test suite,
frontend build, or coverage gate will be run in the agent phase.

## Requirements Checklist

- Add a regression test for the `ComposeOperationError` handler proving a
  cleanup-won check failure does not mask the compose error and still marks the
  workspace failed.
- Add a regression test for the catch-all handler proving a cleanup-won check
  failure does not mask the original unexpected launch error and still marks the
  workspace failed.
- Wrap cleanup-won checks inside exception handlers so secondary cleanup-check
  failures are logged and fall through to `_mark_failed()`.
- Preserve the existing early-return behavior when cleanup actually won.
- Run only focused tests and lint for changed files.

## Implementation Steps

1. Add failing regression tests in the existing provisioner failure-path test
   modules.
2. Run the focused new tests to confirm they fail before the code change.
3. Add a small provisioner helper that guards `_launch_lost_to_terminal_cleanup`
   for exception-handler use and returns `False` after logging secondary
   failures.
4. Replace the unguarded cleanup-won checks in the `ComposeOperationError` and
   catch-all handlers with the guarded helper.
5. Re-run focused tests and a narrow lint check for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestComposeFailureTerminalCleanupRace::test_compose_failure_marks_failed_when_terminal_cleanup_check_raises tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestFailureHandlingEdges::test_unexpected_launch_failure_marks_failed_when_terminal_cleanup_check_raises -q`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestComposeFailureTerminalCleanupRace -q`
  - Passes after implementation to cover preserved cleanup-won behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  - Passes with no lint errors.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
