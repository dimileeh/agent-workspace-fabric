# Capacity Queue Provider Suppression Refill Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6Dzzc4_CAPACITY_QUEUE_REFILL_PLAN.md`

## Requirement Status

- Preserve scheduler-order candidate selection for capacity queue blocker diagnostics: Complete.
  - Evidence: `src/awf/service/metrics.py` continues to load candidates through `_capacity_queue_candidates`, using the existing scheduler SQL ordering and Python re-sort before blocker evaluation.
- Refill the blocker candidate scan after provider cooldown or open-circuit suppression removes candidates, up to the configured scan limit or queue exhaustion: Complete.
  - Evidence: `_provider_recovery_eligible_capacity_queue_scan_candidates` pages through ordered queue candidates until the eligible candidate frontier is filled or the queue is exhausted.
- Keep provider-suppressed candidates excluded from blocker counts: Complete.
  - Evidence: existing cooldown and circuit tests still pass, and the refill path delegates each page to `_provider_recovery_eligible_capacity_queue_candidates`.
- Add regression coverage for a suppressed queue head followed by an eligible capacity-blocked workspace: Complete.
  - Evidence: `test_capacity_queue_blocked_reason_counts_refills_after_provider_suppression` covers cooldown and open-circuit suppressed rows ahead of an eligible DIND-blocked row.
- Run focused tests for the changed metrics behavior: Complete.
  - Evidence: commands below.
- Commit only files changed for this thread with a conventional commit message referencing the review thread id: Complete.
  - Evidence: this validation file is prepared for the final staged commit for review thread `PRRT_kwDOSJAM6s6Dzzc4`.

## Verification Evidence

- Initial regression check before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "refills_after_provider_suppression"`
  - Result: failed with `{}` instead of `{"DIND_CAPACITY_SATURATED": 1}`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "refills_after_provider_suppression"`
  - Result: passed.
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  - Result: passed, `9 passed, 90 deselected`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  - Result: passed.
  - `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.

## Gaps

No implementation gaps remain.
