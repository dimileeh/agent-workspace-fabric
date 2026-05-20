# PRRT_kwDOSJAM6s6Da11 Capacity Scan Resume Validation

## Plan Reference

- `plans/PRRT_kwDOSJAM6s6Da11_CAPACITY_SCAN_RESUME_PLAN.md`

## Requirement Status

- Preserve the existing per-poll bound for fully blocked requested queues while
  holding the local capacity scheduler lock: Complete. Existing
  `test_requested_capacity_gate_bounds_fully_blocked_page_scan` remains green
  in the requested-capacity suite.
- Avoid permanent starvation by resuming the next poll after the last scanned
  blocked page when local allocated capacity has not changed: Complete. Added
  `test_requested_capacity_gate_resumes_after_bounded_blocked_scan`.
- Reset the resume cursor when allocated capacity changes, the requested queue
  is exhausted, or provisioning slots are filled: Complete. The resume cursor
  is stored with an allocated-capacity signature and omitted from the result
  when the scan reaches the end or fills provision slots.
- Keep existing scheduler ordering within each scanned page, queue-decision
  recording, and max-concurrent provisioning semantics: Complete. The capacity
  path still reads scheduler-ordered pages and reuses
  `_claim_requested_capacity_candidates`.
- Add a regression test where a fitting requested workspace beyond the bounded
  scan window is claimed on a later poll: Complete.

## Evidence

Changed files:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6Da11_CAPACITY_SCAN_RESUME_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Da11_CAPACITY_SCAN_RESUME_VALIDATION.md`

Validation commands:

- Red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_resumes_after_bounded_blocked_scan -q`
  failed before implementation with `assert 0 == 1` on the second poll.
- Focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_resumes_after_bounded_blocked_scan -q`
  passed.
- Requested-capacity suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate"`
  passed with `13 passed, 188 deselected`.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passed.
- Whitespace:
  `git diff --check`
  passed.

## Gaps

No gaps remain against the saved plan.
