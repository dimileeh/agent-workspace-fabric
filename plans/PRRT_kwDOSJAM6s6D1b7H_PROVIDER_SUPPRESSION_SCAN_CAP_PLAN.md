# PRRT_kwDOSJAM6s6D1b7H Provider Suppression Scan Cap Plan

## Problem Statement and Scope

The capacity queue `blocked_reason_counts` diagnostic refills its candidate
window after provider-recovery suppression. When many requested workspaces are
provider-suppressed, the refill loop can scan the whole requested queue. This
must remain bounded on the metrics hot path.

Scope is limited to `src/awf/service/metrics.py`, targeted regression coverage
in `tests/unit/service/test_metrics.py`, and this plan/validation record.

## Requirements Checklist

- Add a hard page/row bound for provider-suppression refill scans.
- Preserve the existing ability to refill past a small number of suppressed
  candidates within that bound.
- Make truncation behavior explicit in code comments.
- Add a regression test that fails without the page cap.
- Run the narrow affected tests and static checks appropriate for the touched
  files.

## Implementation Steps

1. Add a failing unit test that sets a small candidate page size and refill page
   cap, fills the front of the queue with provider-cooldown-suppressed rows, and
   asserts the metrics path only issues the capped number of queue page reads.
2. Introduce a refill page cap constant and pass it into
   `_provider_recovery_eligible_capacity_queue_scan_candidates`.
3. Stop the refill loop once the page cap is reached, returning only the
   eligible candidate prefix gathered inside the scan window.
4. Re-run the regression test, then the neighboring metrics tests and lint/type
   checks as practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any failure is unrelated and documented.
