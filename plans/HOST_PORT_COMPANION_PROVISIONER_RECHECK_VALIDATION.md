# Host Port Companion Provisioner Recheck Validation

Plan reference: `plans/HOST_PORT_COMPANION_PROVISIONER_RECHECK_PLAN.md`

## Requirement Status

- Complete: Companion host ports from `task_policy` are checked in the provisioner before Compose launch.
  - Evidence: `Provisioner._check_companion_host_ports` extracts companion host ports, acquires the admission lock, and calls `find_host_port_conflicts` before companion materialization.

- Complete: The provisioner excludes the current workspace from conflict scanning but still detects the source workspace when its runtime is unreleased.
  - Evidence: `_check_companion_host_ports(..., excluding_workspace_id=workspace_id)` is called from the provisioning path and the focused regression seeds a failed unreleased source workspace on the same node.

- Complete: A conflict marks the retry workspace failed before stack launch/materialization side effects that depend on Compose.
  - Evidence: `test_companion_host_port_conflict_fails_before_stack_launch` asserts no stack-launch request, no companion worktree, no `compose_project_name`, and a `COMPANION_HOST_PORT_CHECK_FATAL` event.

- Complete: Existing duplicate/conflict error classes and reason-code logging patterns are preserved.
  - Evidence: The new helper raises `WorkspaceCreateDuplicateHostPortError` and `WorkspaceCreateHostPortConflictError`, and the caller follows the existing auto-profile host-port failure pattern with a companion-specific reason code.

- Complete: Retry-path comments no longer claim companion ports are not rechecked by the provisioner.
  - Evidence: `src/awf/service/workspaces_retry.py` now documents that companion and auto-resolved profile ports are both rechecked before Compose launch.

- Complete: Focused tests demonstrate the race is handled before `stack_launcher.launch`.
  - Evidence: See test commands below.

## Deferred Follow-Up

The review comment's full-table-scan scalability concern is valid but non-blocking for this race fix. The existing repository docstring already documents the scan and likely remedies; implementing a JSONB/GIN index or denormalized `host_ports int[]` column requires schema/index design and migration work. Full tracking or prioritization should be handled outside this review-comment fix.

## Verification Commands

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_companion_host_port_conflict_fails_before_stack_launch -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_005.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/service/workspaces_retry.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py tests/unit/node/test_provisioner_parts/test_provisioner_part_005.py`
- Passed: `uv run --python 3.12 --extra dev mypy src/awf/node/provisioner.py src/awf/service/workspaces_retry.py`

Full AWF/GitHub validation is managed by AWF after agent completion and was not run inside this workspace phase.
