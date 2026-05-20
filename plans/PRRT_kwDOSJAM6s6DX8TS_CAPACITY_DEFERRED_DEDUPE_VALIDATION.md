# PRRT_kwDOSJAM6s6DX8TS Capacity Deferred Dedupe Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DX8TS_CAPACITY_DEFERRED_DEDUPE_PLAN.md`

## Requirement Status

- Complete: the first local capacity deferral is still recorded for a blocked
  requested workspace.
- Complete: repeated unchanged deferred capacity decisions for the same attempt
  are skipped when the latest queue decision has the same decision, reason code,
  and capacity blocker signature.
- Complete: changed blocker details still create a new deferred capacity
  decision.
- Complete: ordered/defaulted capacity decisions and non-capacity queue decision
  behavior are unchanged by scoping the duplicate guard to deferred capacity
  writes only.
- Complete: focused regression coverage was added for repeated unchanged
  capacity deferrals, plus preservation coverage for changed blocker details.
- Complete: focused tests, full worker unit tests, lint, and type checks passed.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DX8TS_CAPACITY_DEFERRED_DEDUPE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DX8TS_CAPACITY_DEFERRED_DEDUPE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k repeated_unchanged_capacity_deferral`
  - Result before implementation: failed because two `LOCAL_CAPACITY_DEFERRED`
    rows were recorded after two unchanged polls.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k repeated_unchanged_capacity_deferral`
  - Result after implementation: passed, `1 passed, 191 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k capacity_gate`
  - Result: passed, `6 passed, 187 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Result: passed, `193 passed`.
- `uv run --python 3.12 --extra dev ruff format src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: formatted both touched files after the commit hook reported format
    drift.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k capacity_gate`
  - Result after formatting: passed, `6 passed, 187 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result after formatting: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result after formatting: passed.

## Gaps

No planned gaps remain.
