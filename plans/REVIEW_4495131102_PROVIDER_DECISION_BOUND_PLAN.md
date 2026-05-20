# REVIEW 4495131102 Provider Decision Bound Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` reports that the local-capacity requested-workspace claim loop passes the whole fetched page into provider-recovery filtering, causing provider cooldown/circuit `QueueDecision` rows to be written for candidates beyond the current provisioning claim slots.

Scope is limited to the capacity scheduling path in `src/awf/control/worker.py` and focused unit coverage in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test showing the capacity gate still records provider deferral for a suppressed candidate that must be skipped to fill a claim slot.
- Add a regression test assertion showing provider-suppressed candidates after the current claim window do not receive unnecessary deferred `QueueDecision` rows.
- Preserve capacity scheduling correctness, including scanning past local-capacity blockers and provider-suppressed candidates to claim eligible work.
- Keep existing non-capacity scheduler behavior compatible with current tests.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add the failing capacity-path regression test.
2. Run the narrow test and confirm it fails on the extra provider-suppression decision.
3. Add bounded provider-recovery decision recording while still filtering all candidates for eligibility.
4. Pass the remaining capacity claim slots as the provider decision-recording bound in the local-capacity loop.
5. Re-run the targeted tests and relevant lint/type checks as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "provider_suppression_decisions_to_claim_slots or dispatches_oldest_satisfiable_candidate or provider_cooldown_defer_does_not_consume_ready_execution_limit"`
  - Passes and shows the regression is fixed without breaking representative capacity and provider-cooldown behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes with no lint regressions.
