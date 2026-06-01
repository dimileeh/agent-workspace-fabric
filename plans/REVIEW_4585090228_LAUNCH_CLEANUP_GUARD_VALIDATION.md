# Review 4585090228 Launch Cleanup Guard Validation

Plan reference: `plans/REVIEW_4585090228_LAUNCH_CLEANUP_GUARD_PLAN.md`

## Requirement Status

- Complete: Added a `ComposeOperationError` regression test proving a
  `_launch_lost_to_terminal_cleanup()` failure does not mask the compose error
  and the workspace is marked failed.
- Complete: Added a catch-all launch-failure regression test proving a cleanup
  check failure does not mask the original unexpected launch error and the
  workspace is marked failed.
- Complete: Added `_launch_lost_to_terminal_cleanup_best_effort()` and wired it
  into the `ComposeOperationError` and catch-all exception handlers.
- Complete: Preserved the existing early-return behavior when terminal cleanup
  actually won; the existing cleanup-race tests still pass.
- Complete: Ran focused tests and lint only. Full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
- `plans/REVIEW_4585090228_LAUNCH_CLEANUP_GUARD_PLAN.md`

Focused checks:

- Failing-before evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestComposeFailureTerminalCleanupRace::test_compose_failure_marks_failed_when_terminal_cleanup_check_raises tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestFailureHandlingEdges::test_unexpected_launch_failure_marks_failed_when_terminal_cleanup_check_raises -q`
  failed with two failures because the secondary cleanup-check `RuntimeError`
  replaced the original compose/unexpected launch failures.
- Passing-after evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestComposeFailureTerminalCleanupRace::test_compose_failure_marks_failed_when_terminal_cleanup_check_raises tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestFailureHandlingEdges::test_unexpected_launch_failure_marks_failed_when_terminal_cleanup_check_raises -q`
  passed: `2 passed`.
- Existing cleanup-race coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestComposeFailureTerminalCleanupRace -q`
  passed: `4 passed`.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  passed with no issues.

## Gaps

None. Broader repository validation, coverage gates, and GitHub checks were not
run in the agent phase per the AWF workspace contract.
