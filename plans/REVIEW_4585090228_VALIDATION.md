# Review 4585090228 Validation

Plan reference: `plans/REVIEW_4585090228_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing an already-over-cap revoke count still records `REVOKE_CAP_REACHED` when orphan stop fails.
  - Evidence: `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation`.
  - Pre-fix result: the new test failed because no `REVOKE_CAP_REACHED` event was recorded when the next revoke count was 5.
- Complete: Change the revoke escalation threshold from exact equality to an inclusive threshold.
  - Evidence: `src/awf/node/provisioner.py` now uses `revoke_count + 1 >= _MAX_REVOKE_EVENTS`.
- Complete: Clarify `_check_companion_host_ports` documentation.
  - Evidence: `src/awf/node/provisioner.py` now documents that its advisory lock is transaction-scoped, released before Docker Compose launch, and is a defense-in-depth database recheck rather than a Docker-bind-duration lock.
- Complete: Run only focused validation for the changed behavior/files.
  - Evidence: focused commands listed below passed. Full AWF/GitHub validation is managed by AWF after agent completion per the workspace contract.
- Complete: Commit the focused fix locally without pushing.
  - Evidence: to be satisfied by the local commit after this validation document is written.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation -q`
  - Failed before the implementation change for the expected missing escalation event.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_failure_over_cap_records_revoke_cap_escalation -q`
  - Passed after implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q`
  - Passed: `18 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  - Passed: `All checks passed!`.

## Remaining Gaps

None for the planned scope. Full repository validation, coverage gates, and CI-equivalent checks were intentionally not run in the agent phase.
