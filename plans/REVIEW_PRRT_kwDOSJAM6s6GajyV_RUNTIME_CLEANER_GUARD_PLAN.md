# Review PRRT_kwDOSJAM6s6GajyV Runtime Cleaner Guard Plan

## Problem Statement and Scope

The review thread reports that `_release_terminal_runtime_resources` still runs the planning-scope auto-retry resume scan when `self._runtime_cleaner` is `None`. That scan can call the retry path even though this worker is not configured to clean terminal runtimes.

Scope is limited to the terminal-runtime release worker path and a focused regression test.

## Requirements Checklist

- Confirm the review feedback is actionable against current code.
- Add a regression test proving a worker without a runtime cleaner does not scan or resume planning-scope auto-retries.
- Restore the runtime-cleaner guard so terminal runtime release work, including dependent planning retry resume scans, is skipped when no cleaner is configured.
- Run focused tests only; full AWF/GitHub validation remains owned by AWF after agent completion.
- Record validation evidence in a matching validation document.

## Implementation Steps

1. Add a narrow unit test around `_release_terminal_runtime_resources` with `_runtime_cleaner=None` and a positive limit.
2. Confirm that test fails against the current behavior.
3. Update `src/awf/control/worker/cleanup.py` to return early when `_runtime_cleaner` is `None`.
4. Run the focused regression test and nearby cleanup worker tests touched by the change.
5. Commit the fix locally with the review thread id in the message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py -q -k "release_terminal_runtime_resources"`
  - Passes, including the new no-cleaner regression.
- Full AWF/GitHub validation is not run locally per the workspace contract.
