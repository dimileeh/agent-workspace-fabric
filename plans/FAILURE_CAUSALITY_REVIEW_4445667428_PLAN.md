# Failure Causality Review 4445667428 Plan

## Problem Statement And Scope

Address the review-level feedback on PR comment `issue:4445667428` for failure
causality preservation. The scope is limited to the reported edge cases:

- Same-timestamp epoch reset events must be detected using the same ordering
  tiebreaker as failed state event selection.
- Failed validation runs attached to primary failure snapshots must belong to
  the current failure epoch.
- Cleanup failure result and audit payloads must reuse the secondary failure
  history built by the causality helper instead of constructing a parallel list.

## Requirements Checklist

- Add regression coverage for same-timestamp reset detection.
- Add regression coverage preventing old-epoch validation runs from being
  attached to current validation failures.
- Add regression coverage proving cleanup result/audit secondary history comes
  from `build_preserved_failure_payload`.
- Update failure causality queries without changing unrelated scheduler,
  provider, or state-machine behavior.
- Preserve existing primary/secondary failure payload semantics.

## Implementation Steps

1. Add failing unit tests in `tests/unit/service/test_failure_causality.py` for
   same-timestamp reset ordering and old validation run epoch filtering.
2. Add or update cleanup controls test coverage in
   `tests/unit/service/test_controls.py` to catch a divergence between helper
   secondary history and result/audit payloads.
3. Update `src/awf/service/failure_causality.py` to share event ordering
   predicates for reset-after detection and latest reset-before lookup, then
   filter failed validation runs by the current epoch start.
4. Update `src/awf/service/controls.py` to source result/audit
   `secondary_failure` and `secondary_failures` from the preserved transition
   payload.
5. Run the narrow unit tests for touched behavior, then broader lint/type/unit
   checks as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` must pass.
- `uv run --python 3.12 --extra dev mypy src/awf` must pass.
