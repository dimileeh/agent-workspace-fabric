# Comment 4567835183 Admission Lock Validation

Plan reference: `plans/COMMENT_4567835183_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing a named worker claim waits while
  the legacy/null-node `local` requested-admission lock is held.
- Complete: Updated requested-admission locking so named workers acquire the
  `local` lock and their named lock in deterministic order.
- Complete: Existing focused admission behavior remains covered by the
  admission/requested-claim subset.
- Complete: Verification used targeted local checks only. Full AWF/GitHub
  validation remains post-agent owned.

## Evidence

Files changed:

- `src/awf/control/worker/admission.py`
- `src/awf/control/worker/claims.py`
- `tests/unit/control/test_worker_scheduler_admission.py`
- `plans/COMMENT_4567835183_PLAN.md`
- `plans/COMMENT_4567835183_VALIDATION.md`

Commands run:

- Failing pre-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "named_worker_admission_waits_for_null_node_lock"`
  failed with the claim completing while the `local` lock was still held.
- Passing post-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "named_worker_admission_waits_for_null_node_lock"`
  passed.
- Focused admission subset:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "admission or requested_claim"`
  passed: 15 tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/admission.py src/awf/control/worker/claims.py tests/unit/control/test_worker_scheduler_admission.py`
  passed.
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/worker/admission.py src/awf/control/worker/claims.py`
  passed.

## Gaps

None. Broad repository validation, coverage gates, and CI-equivalent commands
were intentionally not run inside the agent phase per the AWF workspace
contract.
