# Review 4495131102 Cursor Composition And UI Validation

Plan reference:
`plans/REVIEW_4495131102_CURSOR_COMPOSITION_AND_UI_PLAN.md`

## Requirement Status

- Add a regression test proving same-count requested queue replacement changes
  the queue signature even when max `updated_at`, max `created_at`, and max id
  are unchanged: Complete. `tests/unit/control/test_worker.py` now covers the
  stable-aggregate replacement case.
- Update `_RequestedCapacityQueueSignature` so a queue composition change
  invalidates a saved resume cursor in that edge case: Complete.
  `src/awf/control/worker.py` now includes a stable requested workspace-id
  digest in the queue signature.
- Gate the console `Oldest queued` fact so it only appears when a queued
  workspace is actually waiting: Complete.
  `apps/console/components/console-dashboard.tsx` renders the fact only when
  queued count is positive or a wait duration is present.
- Keep changes scoped to the cited worker and console areas: Complete. Changes
  are limited to the cited implementation files, focused tests, and plan
  artifacts.

## Evidence

- Initial failing worker regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  failed because the before/after signatures were identical for same-count
  composition replacement.
- Initial failing console regression:
  `npm --prefix apps/console run test -- console-dashboard-source.test.mjs`
  failed because `Oldest queued` was rendered unconditionally.
- Passing focused worker check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  passed with 2 tests.
- Passing focused console source check:
  `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/console-dashboard-source.test.mjs`
  passed with 2 tests.
- Passing worker surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed with 214 tests.
- Passing static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- Passing type checks:
  `uv run --python 3.12 --extra dev mypy src/awf` passed.
  `npm --prefix apps/console run typecheck` passed.
- Passing console lint:
  `npm --prefix apps/console run lint` passed.

## Gaps

None.
