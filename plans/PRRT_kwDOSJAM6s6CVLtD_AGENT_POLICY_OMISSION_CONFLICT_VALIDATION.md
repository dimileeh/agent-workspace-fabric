# PRRT_kwDOSJAM6s6CVLtD Agent Policy Omission Conflict Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CVLtD_AGENT_POLICY_OMISSION_CONFLICT_PLAN.md`

## Requirement Status

- Complete: Updated the focused regression so omitted replay `model`/`effort`
  conflicts with existing live adoption `agent_model` or `agent_effort` policy.
- Complete: Preserved idempotent attachment for explicit replay of the same
  model-only policy after effort defaulting.
- Complete: Preserved explicit mismatch conflicts for requested model or effort
  values through the existing service coverage.
- Complete: Clarified adoption docs and REST API reference text so model/effort
  overrides are part of the raw monitor policy conflict contract.
- Complete: Kept changes scoped to PR monitor adoption policy comparison,
  focused tests, and public policy wording.

## Evidence

Files changed:

- `src/awf/service/pr_monitor_adoption.py`
- `tests/unit/service/test_pr_monitor_adoption.py`
- `docs/PR_MONITOR_ADOPTION.md`
- `docs/REST_API_REFERENCE.md`
- `plans/PRRT_kwDOSJAM6s6CVLtD_AGENT_POLICY_OMISSION_CONFLICT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CVLtD_AGENT_POLICY_OMISSION_CONFLICT_VALIDATION.md`

Failing-first command:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_conflicts_with_policy_bearing_adoption -q`
  failed before implementation because no `PRMonitorAdoptionError` was raised.

Passing validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_conflicts_with_policy_bearing_adoption -q`
  passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py -q`
  passed: 102 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/pr_monitor_adoption.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_pr_monitor_adoption_docs.py -q`
  passed: 9 tests.

## Gaps

None.
