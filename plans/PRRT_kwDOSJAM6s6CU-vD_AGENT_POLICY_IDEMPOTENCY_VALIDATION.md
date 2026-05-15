# PRRT_kwDOSJAM6s6CU-vD Agent Policy Idempotency Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CU-vD_AGENT_POLICY_IDEMPOTENCY_PLAN.md`

## Requirement Status

- Complete: Add a failing regression proving a repeat adoption with omitted
  model/effort attaches to an existing live adoption that already has agent
  policy.
- Complete: Preserve explicit mismatch conflicts for requested model or effort
  values.
- Complete: Make the conflict check compare only agent policy keys constrained
  by the current request after request-side default effort resolution.
- Complete: Avoid unrelated PR monitor adoption, workspace creation, or monitor
  runtime changes.

## Evidence

Files changed:

- `src/awf/service/pr_monitor_adoption.py`
- `tests/unit/service/test_pr_monitor_adoption.py`
- `plans/PRRT_kwDOSJAM6s6CU-vD_AGENT_POLICY_IDEMPOTENCY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CU-vD_AGENT_POLICY_IDEMPOTENCY_VALIDATION.md`

Failing-first command:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_attaches_to_policy_bearing_adoption -q`
  failed before implementation with `PR_ADOPTION_POLICY_CONFLICT` for
  `agent_model`.

Passing validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_replay_omitting_agent_policy_attaches_to_policy_bearing_adoption -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py -q`
  passed: 101 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/pr_monitor_adoption.py`
  passed.

## Gaps

None.
