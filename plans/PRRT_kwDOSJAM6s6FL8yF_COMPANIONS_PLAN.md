# PRRT_kwDOSJAM6s6FL8yF Companions Plan

## Problem Statement and Scope

The PR review thread reports that `workspace_create_task_policy_snapshot` omits the
`companions` task-policy key when the request has no companions. Idempotency reads a
missing key as an empty list, while requested companions are always serialized as a
list. This plan covers only making the task-policy snapshot symmetric for the empty
companion list and adding targeted regression coverage.

## Requirements Checklist

- Persist `companions: []` in workspace create task-policy snapshots for requests
  with no companions.
- Preserve the existing serialized companion representation for non-empty
  companion lists.
- Keep the change scoped to workspace create policy snapshot behavior and its
  focused unit tests.
- Use targeted validation only; full AWF/GitHub validation is managed after agent
  completion.

## Implementation Steps

1. Add or update a focused unit test that fails while the empty companion list is
   omitted from `workspace_create_task_policy_snapshot`.
2. Update the snapshot builder to always store `_requested_companions(payload)` under
   `COMPANION_POLICY_KEY`.
3. Update any exact policy assertions affected by the explicit empty list.
4. Run the focused unit test file or individual tests that cover the changed policy
   behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q -k "task_policy_and_profile_tier_helpers or companion"`
  - Passes with the new empty-companion regression and existing snapshot assertions.

Full repository validation, coverage, and CI-equivalent commands are intentionally
left to AWF/GitHub after this agent phase.
