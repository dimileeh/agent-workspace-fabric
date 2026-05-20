# AWF Monitor Provider Retry Stability Plan

## Problem

Retry workspaces `ws_8e051db584564e9d9bd97566` and
`ws_eba3d6c717f3491bb4d2c367` validated, pushed, opened PRs, and then failed
as `STALE_ACTIVE_EXECUTION` while in `monitoring_pr`.

## Root Cause

The PR monitor hit the provider circuit-open fast path while addressing PR
comments. That path wrote `workspace.provider_recovery_cooldown` but did not
persist `task_policy.provider_recovery_state`. The stale-active scanner only
recognizes pending monitor provider recovery from `task_policy`, so it later
treated the cooldown-paused monitor as an abandoned active runtime and failed
the workspace.

## Fix

Persist a minimal monitor in-place provider recovery state whenever the PR
monitor suppresses a CLI call because the provider/model circuit is open:

- `action: retry`
- `decision_reason_code: PROVIDER_MODEL_CIRCUIT_OPEN`
- `source_provider`, `source_model`
- `source_reason_code`
- `not_before`

Advance the workspace version with the mutation and keep the existing cooldown
event for observability.

## Validation

- Add/update a unit test proving `_provider_recovery_suppresses_cli` writes both
  the cooldown event and durable provider recovery state.
- Run focused PR monitor/worker tests for provider recovery and stale-active
  preservation.
- Rebuild/restart AWF and attach fresh PR monitors to PR #265 and PR #266.
