# Review 4495131102 Queue Signature Created-At Validation

Plan reference:
`plans/REVIEW_4495131102_QUEUE_SIGNATURE_CREATED_AT_PLAN.md`

## Requirement Status

- Add a clarifying comment for defaulted-reservation ordered decision dedupe:
  Complete. `src/awf/control/worker.py` now documents that a defaulted
  reservation decision is the ordering record for that attempt.
- Include `MAX(created_at)` in the requested queue signature:
  Complete. `_RequestedCapacityQueueSignature` and
  `_requested_capacity_queue_signature` now include the requested queue's max
  created timestamp.
- Add a regression test that fails against the old three-field signature:
  Complete. `tests/unit/control/test_worker.py` covers unchanged count,
  unchanged max `updated_at`, unchanged max workspace id, and newer replacement
  `created_at`.
- Keep the change local to worker scheduling behavior:
  Complete. Code changes are limited to `src/awf/control/worker.py` and the
  focused worker unit test.

## Evidence

- Initial failing check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  failed because before/after signatures were identical without created-at.
- Passing focused check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  passed.
- Passing worker surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed with 213 tests.
- Passing static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- Passing type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
