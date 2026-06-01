# PRRT_kwDOSJAM6s6GGZfO Stale Cleanup Row Preserve Plan

## Problem Statement

The review thread reports that stale validation cleanup failure recording can
overwrite an already-failed workspace row when failure causality does not return
a primary failure snapshot. The stale cleanup failure is secondary evidence and
must not replace the live terminal failure fields on an already-failed
workspace.

## Scope

- Harden `src/awf/control/executor/execution_validation.py` stale cleanup
  failure recording.
- Add a focused unit regression for the no-primary-causality, already-failed
  workspace path.
- Avoid broad AWF/GitHub-owned validation; run only focused tests covering this
  behavior.

## Requirements

- [ ] Preserve `workspace.failure_reason` and `workspace.failure_message` when
  `_record_stale_validation_cleanup_failure` records secondary cleanup evidence
  for an already-failed workspace without a loaded primary failure.
- [ ] Continue appending secondary cleanup evidence to the emitted
  `workspace.secondary_failure_recorded` payload.
- [ ] Preserve the existing primary-failure restoration behavior when causality
  does load a primary failure.
- [ ] Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add a focused failing regression test for the no-primary-causality fallback.
2. Update stale validation cleanup failure recording so the fallback payload is
   emitted without mutating existing failure row fields.
3. Run targeted pytest for the new test and adjacent existing stale cleanup
   coverage.
4. Record validation evidence in the matching validation artifact.

## Verification

Targeted command:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_validation_stale_cleanup.py \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_stale_validation_cleanup_failure_records_secondary_failure_evidence \
  -q
```

Pass criteria:

- The new regression fails before implementation.
- The targeted tests pass after implementation.
- Full AWF/GitHub validation remains deferred to AWF after agent completion.
