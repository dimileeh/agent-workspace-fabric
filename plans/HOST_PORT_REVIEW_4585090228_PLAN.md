# Host Port Review 4585090228 Plan

## Problem Statement And Scope

PR review comment `issue:4585090228` identified two host-port admission gaps:

- `check_host_port_conflicts` extracts companion host ports with direct tuple
  unpacking, so malformed entries that bypass request validation can raise an
  unhelpful `ValueError`.
- Provisioning skips `_check_auto_resolved_profile_host_ports` when a workspace
  already has `resolved_profile`, so re-provisioning can miss profile-service
  host-port duplicates and profile-port cross-workspace rechecks.

Scope is limited to the service/provisioner host-port admission path and
focused regressions for those behaviors.

## Requirements Checklist

- Add a regression proving malformed companion port entries are skipped by
  `check_host_port_conflicts` while valid entries are still checked.
- Add a regression proving provisioning with an already stored
  `resolved_profile` still runs the profile-service host-port check and fails
  before Compose launch on an intra-workspace duplicate.
- Replace direct companion port tuple unpacking with the existing defensive
  extraction pattern.
- Invoke `_check_auto_resolved_profile_host_ports` for stored-profile
  provisioning attempts as well as newly resolved profiles.
- Keep broad AWF/GitHub validation out of the agent phase; run only focused
  tests for the touched behavior.

## Implementation Steps

1. Update `tests/unit/service/test_host_port_conflict_helper.py` with a
   malformed companion-port regression.
2. Update the focused provisioner failure-path tests with a stored-profile
   re-provisioning regression.
3. Implement defensive companion port extraction in `src/awf/service/workspaces.py`.
4. Remove the provisioner call-site guard that skips profile host-port checking
   when `profile_resolution` is `None`.
5. Run focused pytest node IDs covering the new regressions and nearby helper
   behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_port_conflict_helper.py -q`
  - Passes all helper tests, including malformed companion entries.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_stored_resolved_profile_host_port_conflict_fails_before_stack_launch -q`
  - Passes the stored-profile provisioner recheck regression.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py src/awf/node/provisioner.py tests/unit/service/test_host_port_conflict_helper.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
  - Passes focused linting for touched code and tests.

Full repository validation, coverage gates, frontend builds, and GitHub CI are
intentionally left to AWF/GitHub after agent completion per the workspace
contract.
