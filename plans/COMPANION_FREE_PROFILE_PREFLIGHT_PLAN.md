# Companion-Free Profile Preflight Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6FNVaD` reports that `Provisioner` only runs
`validate_companion_service_graph` when companion specs exist. The same
validator also checks profile service dependency targets and dependency
healthcheck capability, so profiles with no companions can skip provisioner
preflight and fail later in stack launch after secret leases are issued.

Scope is limited to the provisioning preflight path and focused regression
coverage for companion-free profile service dependency validation.

## Requirements Checklist

- Add a regression test showing a profile-only invalid dependency graph fails
  before stack launch.
- Prove profile secret leases are not issued before this preflight failure.
- Update provisioner preflight to validate profile services even when the
  workspace has zero companion specs.
- Preserve companion materialization behavior and the existing stack-launch
  request contract.
- Run only focused tests for the changed behavior; broad AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Add a targeted unit test in the provisioner tests for a companion-free
   profile whose service depends on a target without a healthcheck.
2. Confirm the new test fails against the current provisioner behavior.
3. Update `src/awf/node/provisioner.py` so graph validation runs before
   companion materialization and secret lease issuance regardless of companion
   count when a stack launcher is present.
4. Run the focused regression test and a nearby companion preflight test.
5. Record validation evidence in `plans/COMPANION_FREE_PROFILE_PREFLIGHT_VALIDATION.md`.
