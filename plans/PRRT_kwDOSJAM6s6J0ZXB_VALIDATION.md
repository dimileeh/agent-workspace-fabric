# PRRT_kwDOSJAM6s6J0ZXB Validation

## Change
Wired the pre-PR `blocked`-resume path into the worker loop:
- `src/awf/db/repositories/workspace_repo.py`: `list_resumable_blocked_ids()`
  (eligibility = directive armed OR active-epoch grant; excludes undecided).
- `src/awf/control/worker/claims.py`: `_claim_blocked_resume_ids()` batch claim.
- `src/awf/control/worker/manager.py`: `_list_resumable_blocked()` + `run_once()`
  dispatch block between monitor resumes and ready executions, recording an
  `ORDERED_BLOCKED_RESUME` queue decision.
- `src/awf/control/worker/mixins.py`: bound `_claim_blocked_for_resume`,
  `_claim_blocked_resume_ids`, `_dispatch_blocked_resumes`,
  `_safely_resume_blocked_claimed`.

## Focused checks (agent phase)
- `ruff check` + `ruff format --check` on all changed files — pass.
- `mypy` (full `src/`, pyproject-pinned) — `Success: no issues found in 385 source files`.
- `pytest tests/unit/db/test_operator_grants.py` — 3 passed (new repo eligibility test).
- `pytest tests/unit/control/test_worker_blocked_resume.py` — 3 passed
  (resume on directive, resume on grant, no-op when undecided; asserts
  `blocked -> running` + executor `resume_blocked_execution` invoked).
- `pytest tests/unit/control/test_blocked_status_membership.py
  tests/unit/control/test_enter_blocked_for_protected_violation.py` — pass.
- `pytest tests/unit/control/test_worker_parts` — pass (no regression in the
  existing dispatch/recovery paths).

## Notes
Broad validation, coverage gate, and OpenAPI drift are owned by AWF/GitHub CI
after the agent phase, per the workspace contract.
