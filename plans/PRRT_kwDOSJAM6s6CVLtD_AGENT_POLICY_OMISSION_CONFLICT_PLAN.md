# PRRT_kwDOSJAM6s6CVLtD Agent Policy Omission Conflict Plan

## Problem Statement And Scope

An unresolved PR review thread reports that live PR monitor adoption replay
checks treat omitted `model` and `effort` request fields as unconstrained. A
later adoption of the same live PR can therefore attach to a workspace whose
`task_policy` carries a previous custom `agent_model` or `agent_effort`, even
though the later caller requested the default/no-override policy.

Scope is limited to PR monitor adoption idempotency policy comparison,
regression coverage, and public adoption-policy wording.

## Requirements Checklist

- Add or update a failing regression proving omitted `model`/`effort` conflicts
  with an existing live adoption that has the corresponding agent policy.
- Preserve idempotent attachment when the replay explicitly requests the same
  model-only policy after effort defaulting.
- Preserve explicit mismatch conflicts for requested model or effort values.
- Clarify adoption docs so agent model/effort overrides are part of the monitor
  policy conflict contract.
- Avoid unrelated PR monitor adoption, workspace creation, or monitor runtime
  changes.

## Implementation Steps

1. Update the focused service replay test to expect `PR_ADOPTION_POLICY_CONFLICT`
   when a replay omits agent policy but the live adoption has `agent_model` or
   `agent_effort`.
2. Run that focused test before code changes and confirm it fails.
3. Update `_raise_if_agent_policy_conflicts` to compare both known agent policy
   keys, treating absence on either side as part of the raw policy.
4. Update PR monitor adoption docs/API reference wording for model/effort policy
   conflicts.
5. Re-run the focused test, related PR monitor adoption service tests, and
   narrow lint/type checks for touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_conflicts_with_policy_bearing_adoption -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py -q`
  should pass for the full PR monitor adoption service surface.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py`
  should report no lint violations in the touched Python files.
- `uv run --python 3.12 --extra dev mypy src/awf/service/pr_monitor_adoption.py`
  should pass, or any limitation must be documented in validation.
