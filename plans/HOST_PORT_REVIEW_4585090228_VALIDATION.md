# Host Port Review 4585090228 Validation

Plan reference: `plans/HOST_PORT_REVIEW_4585090228_PLAN.md`

## Requirement Status

- Complete: Add a regression proving malformed companion port entries are
  skipped by `check_host_port_conflicts` while valid entries are still checked.
- Complete: Add a regression proving provisioning with an already stored
  `resolved_profile` still runs the profile-service host-port check and fails
  before Compose launch on a cross-workspace profile-port conflict.
- Complete: Replace direct companion port tuple unpacking with defensive
  extraction in `src/awf/service/workspaces.py`.
- Complete: Invoke `_check_auto_resolved_profile_host_ports` for stored-profile
  provisioning attempts as well as newly resolved profiles in
  `src/awf/node/provisioner.py`.
- Complete: Keep validation focused during the agent phase.

## Evidence

Files changed:

- `src/awf/service/workspaces.py`
- `src/awf/node/provisioner.py`
- `tests/unit/service/test_host_port_conflict_helper.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
- `plans/HOST_PORT_REVIEW_4585090228_PLAN.md`

Red phase confirmed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_port_conflict_helper.py::TestCheckHostPortConflictsIntraRequestDuplicate::test_malformed_companion_port_entries_are_skipped -q`
  failed with `ValueError: not enough values to unpack`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_stored_resolved_profile_host_port_conflict_fails_before_stack_launch -q`
  failed because the stack launcher was invoked.

Green phase:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_port_conflict_helper.py -q`
  passed: `37 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_stored_resolved_profile_host_port_conflict_fails_before_stack_launch -q`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py src/awf/node/provisioner.py tests/unit/service/test_host_port_conflict_helper.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py`
  passed: `All checks passed!`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py -q`
  passed: `22 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_005.py -q`
  passed: `4 passed`.

Full AWF/GitHub validation, coverage gates, frontend builds, and CI-equivalent
commands were not run in the agent phase; AWF/GitHub own that validation after
agent completion.

## Gaps

No planned requirements are partial or missing.
