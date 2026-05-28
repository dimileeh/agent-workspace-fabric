# Duplicate Companion Graph Validation Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6FNVZ7` reports that companion service graph
validation now runs twice for provisioned workspaces with companions: once in
the provisioner preflight before companion worktrees are materialized, and once
again in `ComposeStackLauncher.launch`.

## Scope

- Avoid the duplicate validation on the provisioner companion path.
- Preserve the launcher's validation for direct launcher use and profile-only
  dependency checks.
- Keep the fix internal to the provisioner/launcher contract.

## Requirements Checklist

- [x] Provisioner requests with companions mark the companion graph as already
      prevalidated after the pre-materialization check succeeds.
- [x] `ComposeStackLauncher` skips the companion graph validation only when a
      launch request with materialized companions is marked prevalidated.
- [x] Default direct `ComposeStackLauncher` calls still validate companion and
      profile service graph errors.
- [x] Validation is focused; full AWF/GitHub validation remains managed after
      agent completion.

## Implementation Steps

1. Add focused tests for the provisioner marker and launcher skip behavior.
2. Add an internal boolean to `WorkspaceStackLaunchRequest` with a safe default.
3. Set the marker in the provisioner only after companion preflight validation.
4. Gate the launcher validation call on the marker and the presence of
   materialized companions.
5. Run focused unit tests for the changed behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q -k "prevalidated or preflights_profile_dependencies_without_companions or rejects_companion_profile_service_collision"`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q -k "materializes_companion_worktrees_before_stack_launch or rejects_invalid_companion_graph_before_materializing_companions"`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py src/awf/node/provisioner.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`

Full AWF/GitHub validation is managed by AWF after agent completion and will not
be run in this agent phase.
