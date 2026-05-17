# Review Thread PRRT_kwDOSJAM6s6CsIvR Plan

## Problem Statement and Scope

An unresolved PR review thread reports that workspace-create idempotency replays
reject legacy rows created by the flat v1 API when the row stores
`env_profile`, but not `profile_ref`, `requested_profile`, or `resolved_profile`.
The current profile-ref matcher accepts this legacy shape, while the legacy
unknown requested-tier fallback does not.

Scope is limited to workspace-create idempotency matching for legacy
`env_profile` rows.

## Requirements Checklist

- Add a regression test proving a v2 replay with the same `profile_ref` accepts
  a legacy row whose durable profile reference is only `env_profile`.
- Preserve conflicts for non-matching profile refs and existing requested-tier
  mismatch behavior.
- Keep the implementation local to workspace-create replay matching.
- Validate with the narrow unit test file that covers workspace idempotency.

## Implementation Steps

1. Add the failing regression in `tests/unit/service/test_workspace_idempotency.py`.
2. Run the targeted regression to confirm it fails before the fix.
3. Update `src/awf/service/workspaces.py` so the legacy unknown-tier fallback
   accepts a matching legacy `env_profile`.
4. Re-run the targeted idempotency tests.
5. Create a validation document with requirement status and evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  passes.
