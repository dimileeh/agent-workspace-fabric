# Host Port Companion Provisioner Recheck Plan

## Problem Statement

PR review comment `issue:4585090228` flagged that planning-scope auto-retry can bypass the retry-time source-runtime gate for workspaces with companion host ports. Because the provisioner currently rechecks only auto-resolved profile service ports, an auto-retry can reach Docker Compose while the source workspace still has a companion port bound.

The same comment also notes the current host-port conflict query scans active and terminal-unreleased workspace rows linearly. That is a valid scalability follow-up already documented in the repository helper, but fixing it requires schema/index work outside this narrow lifecycle race.

## Scope

- Add a provision-time companion host-port recheck before Docker Compose launch.
- Keep the check node-scoped and protected by the existing per-port advisory lock.
- Fail before stack launch when a source or sibling workspace still owns a companion host port.
- Update stale retry-path comments to reflect that companion ports are now rechecked by the provisioner.
- Add focused regression coverage for the source-unreleased companion-port race.
- Record the linear-scan index/denormalization topic as deferred validation context, without changing schema in this fix.

## Requirements Checklist

- [ ] Companion host ports from `task_policy` are checked in the provisioner before Compose launch.
- [ ] The provisioner excludes the current workspace from conflict scanning but still detects the source workspace when its runtime is unreleased.
- [ ] A conflict marks the retry workspace failed before stack launch/materialization side effects that depend on Compose.
- [ ] Existing duplicate/conflict error classes and reason-code logging patterns are preserved.
- [ ] Retry-path comments no longer claim companion ports are not rechecked by the provisioner.
- [ ] Focused tests demonstrate the race is handled before `stack_launcher.launch`.

## Implementation Steps

1. Add a private provisioner helper for companion host-port rechecks using `host_ports_from_task_policy_companions`, `acquire_host_port_admission_lock`, and `find_host_port_conflicts`.
2. Call the helper in the stack-launch path after companion graph validation and before companion worktree materialization / Compose launch.
3. Reuse the existing failure-handling pattern from `_check_auto_resolved_profile_host_ports` with a companion-specific reason code.
4. Update the retry-path safety note that previously documented the companion-port gap.
5. Add a regression test that seeds an unreleased failed source workspace with a companion port, provisions a retry workspace with the same port, and asserts the launcher is not called.

## Verification

Run only focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py::TestFailureHandling::test_companion_host_port_conflict_fails_before_stack_launch -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_005.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py src/awf/service/workspaces_retry.py tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py tests/unit/node/test_provisioner_parts/test_provisioner_part_005.py`

Full AWF/GitHub validation is intentionally left to AWF after agent completion per workspace contract.
