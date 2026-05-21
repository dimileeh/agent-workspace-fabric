# PRRT_kwDOSJAM6s6DiE9u Resume Suppression Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DiE9u_RESUME_SUPPRESSION_PLAN.md`

## Requirement Status

- Add a regression test for `PROVIDER_RECOVERY_NOT_BEFORE` resume reset:
  Complete. Added
  `test_requested_capacity_gate_resets_resume_cursor_when_provider_suppression_elapses`.
- Reset the requested-capacity resume cursor once an observed provider
  suppression window elapses: Complete. The worker now stores the earliest
  future provider suppression expiry with the resume cursor and refuses to
  reuse the cursor after that time.
- Include provider model circuit breaker cooldowns in the same invalidation
  path: Complete. Added
  `test_requested_capacity_gate_resets_resume_cursor_when_provider_circuit_elapses`.
- Preserve existing cursor reuse for purely capacity-blocked requested work:
  Complete. Existing bounded blocked-scan resume test still passes.
- Keep changes scoped to worker scheduling code and focused tests: Complete.
  Code changes are limited to `src/awf/control/worker.py`; tests are limited
  to `tests/unit/control/test_worker.py`.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`

Validation commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_resets_resume_cursor_when_provider_suppression_elapses"`
  - Failed before implementation with the suppressed requested workspace still
    skipped after cooldown expiry.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_resets_resume_cursor_when_provider_suppression_elapses or requested_capacity_gate_resets_resume_cursor_when_provider_circuit_elapses or requested_capacity_gate_resumes_after_bounded_blocked_scan"`
  - Passed: 3 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_resets_resume_cursor_when_provider_suppression_elapses or requested_capacity_gate_resets_resume_cursor_when_provider_circuit_elapses or requested_capacity_gate_resumes_after_bounded_blocked_scan or provider_recovery_filter_retries_closed_connection or provider_recovery_filter_keeps_scheduler_locks_until_decision_commit"`
  - Passed: 5 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passed: 216 passed.

## Gaps

None.
