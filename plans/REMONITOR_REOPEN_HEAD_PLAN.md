# Remonitor Reopen Head Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6ygS` reports that a past-settle remonitor can freeze
the latest merge-candidate head while failed-workspace recovery reopens that same
candidate with stale `workspace.monitor_last_commit_sha`. The scope is limited to
remonitor recovery for failed workspaces and the regression coverage for that behavior.

## Requirements Checklist

- Reproduce the mismatch where `monitor_last_commit_sha` lags the latest merge-candidate
  `head_sha`.
- Preserve existing past-settle freeze behavior and warning payloads.
- Reopen failed-workspace merge candidates with the same latest candidate head used for
  remonitor settle/freeze evaluation, falling back to workspace monitor SHA only when no
  candidate head is known.
- Keep the fix scoped to remonitor control code and focused tests.
- Do not run broad AWF/GitHub validation; record that AWF owns it after agent completion.

## Implementation Steps

1. Add a unit regression in the remonitor control tests that seeds a failed workspace with
   a stale workspace monitor SHA, a newer closed merge-candidate head, and elapsed settle
   state for the stale head.
2. Confirm the new regression fails because remonitor reopens the candidate with the stale
   workspace SHA.
3. Pass the latest/open candidate head discovered by `remonitor_workspace` into
   `_reset_failed_workspace_for_remonitor`.
4. Update the reset helper to prefer that head when reopening the merge candidate.
5. Run the targeted remonitor test(s) only.

## Verification Commands And Pass Criteria

- Targeted failing check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k remonitor_reopens_failed_candidate_with_latest_head`
- Targeted passing check after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k "remonitor_reopens_failed_candidate_with_latest_head or remonitor_past_settle_persists_operator_hint_and_warns or remonitor_failed_workspace_with_pr_reopens_candidate_for_worker"`

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
