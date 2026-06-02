# Host Port Requested Profile Fallback Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6GY1F5` reports that `find_host_port_conflicts`
does not scan `Workspace.requested_profile` service ports when a queued legacy
or pre-resolution retry has `resolved_profile` set to null. That can admit a
second workspace for the same host port and defer the failure to provisioning.

Scope is limited to repository-level host-port conflict detection and its
focused regression coverage.

## Requirements Checklist

- Add a regression test proving a requested workspace with only
  `requested_profile.services[].ports` blocks the same host port.
- Update `find_host_port_conflicts` to load `Workspace.requested_profile`.
- Use `requested_profile` only as a fallback when `resolved_profile` is null.
- Preserve existing companion, resolved-profile, node-filter, terminal-release,
  and self-exclusion behavior.
- Run focused validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Extend the host-port repository test helper to optionally seed
   `requested_profile`.
2. Add a failing test for a queued/pre-resolution workspace with
   `requested_profile` ports and no `resolved_profile`.
3. Update the host-port scan query and Python profile selection fallback.
4. Run the new regression first, then the adjacent host-port repository test
   file if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_requested_profile_service_port_conflict_before_resolution -q`
  - Passes after the fix and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  - Passes for the adjacent repository surface.

Full repository validation, coverage gates, frontend builds, and CI-equivalent
commands are intentionally not run in the agent phase per the AWF workspace
contract.
