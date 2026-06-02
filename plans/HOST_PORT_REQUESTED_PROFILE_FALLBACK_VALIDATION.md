# Host Port Requested Profile Fallback Validation

Plan reference: `plans/HOST_PORT_REQUESTED_PROFILE_FALLBACK_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving a requested workspace with only
  `requested_profile.services[].ports` blocks the same host port.
- Complete: Update `find_host_port_conflicts` to load
  `Workspace.requested_profile`.
- Complete: Use `requested_profile` only as a fallback when `resolved_profile`
  is null.
- Complete: Preserve existing companion, resolved-profile, node-filter,
  terminal-release, and self-exclusion behavior.
- Complete: Run focused validation only; broad AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/db/repositories/workspace_repo_host_ports.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`
- `plans/HOST_PORT_REQUESTED_PROFILE_FALLBACK_PLAN.md`
- `plans/HOST_PORT_REQUESTED_PROFILE_FALLBACK_VALIDATION.md`

Focused test evidence:

- Before implementation, the new regression failed with `assert 0 == 1`:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_requested_profile_service_port_conflict_before_resolution -q`
- After implementation, the same regression passed:
  `1 passed in 1.22s`
- Adjacent repository surface passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  reported `36 passed in 28.57s` on the final rerun.
- Focused lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/workspace_repo_host_ports.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`
  reported `All checks passed!`.

Full repository validation, coverage gates, frontend builds, and CI-equivalent
commands were not run in the agent phase per the AWF workspace contract.

## Gaps

No planned gaps remain.
