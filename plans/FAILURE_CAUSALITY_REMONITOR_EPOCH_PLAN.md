# Failure Causality Remonitor Epoch Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6B6qCJ` reports that
`load_primary_failure_snapshot()` can select an older failed event with an
embedded `primary_failure` after a workspace has been remonitored or retried and
then fails again for a different live `workspace.failure_reason`.

Scope is limited to primary failure snapshot selection in
`src/awf/service/failure_causality.py` and targeted unit coverage in
`tests/unit/service/test_failure_causality.py`.

## Requirements Checklist

- Add a regression test for a failed workspace that is reset for remonitoring
  and then fails again with a different current failure reason.
- Preserve existing cleanup behavior where a later cleanup failure can still
  carry the earlier primary failure when no remonitor/retry epoch reset occurred.
- Ignore embedded primary failure payloads from failed events before a failure
  epoch reset such as `workspace.remonitor_requested` to `monitoring_pr`.
- Let the current workspace row and latest failed event supply failure reason,
  message, and reason code after such an epoch reset.
- Keep changes narrow and avoid GitHub writes or branch changes.

## Implementation Steps

1. Add a failing unit test that seeds an embedded validation primary, records a
   remonitor reset to `monitoring_pr`, then fails again as an agent failure.
2. Update failure causality lookup to only reuse older embedded primary evidence
   when no reset into an active/remonitorable execution state occurred after
   that embedded event.
3. Re-run the targeted unit tests.
4. Run the narrow lint check for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passes.
