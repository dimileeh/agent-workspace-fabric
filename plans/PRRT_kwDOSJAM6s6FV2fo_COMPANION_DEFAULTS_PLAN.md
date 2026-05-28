# PRRT_kwDOSJAM6s6FV2fo Companion Defaults Plan

## Problem Statement And Scope

An inline review on PR #292 reports that idempotent workspace-create replays can
conflict after adding the `environment_secrets` companion field. Older stored
`task_policy["companions"]` entries may omit the new default `{}`, while current
request dumps include it.

Scope is limited to replay comparison for stored companion policy entries and a
focused regression test.

## Requirements Checklist

- Confirm the feedback is actionable from the local code.
- Preserve existing requested companion task-policy snapshots.
- Normalize stored companion entries so missing default `environment_secrets`
  compares as `{}` on identical create replays.
- Do not weaken mismatches for genuinely different companion requests.
- Run only focused validation; broad AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add a regression test for `workspace_create_payload_matches` with a stored
   companion entry that omits `environment_secrets`.
2. Add stored-companion normalization in `src/awf/service/workspaces_create.py`.
3. Run the focused regression test file or individual test.
4. Record validation evidence in the matching validation document.
