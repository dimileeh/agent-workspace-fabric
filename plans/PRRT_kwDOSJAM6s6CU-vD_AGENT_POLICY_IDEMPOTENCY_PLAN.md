# PRRT_kwDOSJAM6s6CU-vD Agent Policy Idempotency Plan

## Problem Statement And Scope

An unresolved PR review thread reports that live PR monitor adoption replay
conflict checks compare missing requested `agent_model` and `agent_effort`
values as `None`, so an otherwise idempotent repeat adoption can fail when the
existing live adoption has an agent policy and the new request omits those
optional fields.

Scope is limited to PR monitor adoption live-workspace policy conflict handling
and focused regression coverage.

## Requirements Checklist

- Add a failing regression proving a repeat adoption with omitted model/effort
  attaches to an existing live adoption that already has agent policy.
- Preserve explicit mismatch conflicts for requested model or effort values.
- Make the conflict check compare only agent policy keys constrained by the
  current request after request-side default effort resolution.
- Avoid unrelated PR monitor adoption, workspace creation, or monitor runtime
  changes.

## Implementation Steps

1. Add a focused service test in `tests/unit/service/test_pr_monitor_adoption.py`
   near the existing replay policy tests.
2. Run the new focused test and confirm it fails against the current code.
3. Update `_raise_if_agent_policy_conflicts` in
   `src/awf/service/pr_monitor_adoption.py` to iterate over requested policy
   entries rather than all possible policy keys.
4. Re-run the focused service test and related PR monitor adoption tests.
5. Run narrow lint/type validation for touched Python files if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_attaches_to_policy_bearing_adoption -q`
  passes after failing before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/service/pr_monitor_adoption.py`
  passes or any limitation is documented in validation.
