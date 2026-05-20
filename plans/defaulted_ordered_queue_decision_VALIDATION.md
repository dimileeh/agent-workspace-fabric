# Defaulted Ordered Queue Decision Validation

Plan reference: `plans/defaulted_ordered_queue_decision_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving a defaulted-demand requested workspace claimed by the capacity gate has exactly one `ordered` queue decision.
  - Evidence: `tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_records_one_ordered_decision_for_defaulted_claim`.
- Complete: Preserve the defaulted-reservation reason record so analytics can still see that defaulted demand was used.
  - Evidence: The regression asserts the single ordered decision reason is `LOCAL_CAPACITY_RESERVATION_DEFAULTED`.
- Complete: Keep ordinary requested provisioning, ready execution, and monitor resume ordered-decision behavior unchanged.
  - Evidence: Existing ordered-decision tests passed in the targeted pytest run.
- Complete: Preserve retry dedupe behavior for ambiguous ordered-decision commits.
  - Evidence: Existing ambiguous commit ordered-decision tests passed in the targeted pytest run.
- Complete: Run the narrow affected tests and document validation evidence.
  - Evidence: Commands below.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "records_one_ordered_decision_for_defaulted_claim"`
  - Result: Passed, `1 passed, 202 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "defaulted_ordered or ordered_decision"`
  - Result: Passed, `11 passed, 192 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: Passed, `All checks passed!`.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: Passed, `2 files already formatted`.

## Gaps

No remaining gaps.
