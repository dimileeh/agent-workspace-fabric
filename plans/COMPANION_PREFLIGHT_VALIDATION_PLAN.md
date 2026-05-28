# Companion Preflight Validation Plan

## Problem Statement

The provisioner materializes companion worktrees before the companion/profile
service graph is validated by the stack launcher. Invalid companion graphs can
therefore leave companion worktrees behind even though provisioning fails.

## Scope

- Validate companion/profile service graph inputs before companion worktree
  materialization in `src/awf/node/provisioner.py`.
- Preserve the stack launcher's existing validation as a later guard.
- Add a regression test proving an invalid companion/profile collision fails
  before companion worktrees are created.

## Requirements Checklist

- [x] Invalid companion/profile graphs fail with `ProfileResolutionError` before
      companion `add_worktree` calls.
- [x] Provisioning still marks the workspace as failed with
      `profile_resolution_failure` for preflight graph validation errors.
- [x] Existing successful companion materialization behavior remains unchanged.
- [x] Validation uses the same service graph rules as the launcher.

## Implementation Steps

1. Add a failing provisioner regression test for a companion name that collides
   with a profile service name.
2. Add a pre-materialization companion graph validation call in the provisioner.
3. Adjust companion graph validation types if needed so unmaterialized companion
   specs can be validated.
4. Run focused tests for the provisioner companion behavior and companion graph
   helpers.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "companion"`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`

Full AWF/GitHub validation is managed by AWF after agent completion and will not
be run in this agent phase.
