# PRRT_kwDOSJAM6s6DjqFg Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DjqFg` reports that validation salvage recorded while a workspace is `validating` or `pushing` rewinds the workspace to `running`, but later `running` scans do not recognize the already-recorded salvage marker. When execution slots remain saturated, the worker can record a second preservation and a duplicate validation salvage for the same preserved execution.

Scope is limited to the preserved active execution recovery path in `src/awf/control/worker.py` and focused unit coverage in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add or update a regression test that fails before the fix and covers a rewound non-running validation salvage followed by another saturated `running` scan.
- Preserve existing behavior where a current `running` validation salvage no longer blocks stale failure after preservation grace expires and no execution slot is available.
- Make the recovery path recognize rewound validation salvage markers across `validating`/`pushing` to `running` status transitions without creating duplicate recovery side effects.
- Keep changes scoped to worker recovery logic and tests.

## Implementation Steps

1. Extend the existing rewound validation salvage test to run a second saturated scan before a validation slot is available.
2. Confirm the test fails with duplicate preservation or duplicate validation salvage on the current code.
3. Update worker recovery logic to recognize a pending rewound validation salvage while execution slots are unavailable and return without recording another preservation/salvage cycle.
4. Keep normal current-running preservation expiration behavior intact.
5. Run the targeted test, then a narrow worker test subset if time permits.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k rewound_validation_salvage`
  - Passes with a single validation salvage event, a single preserved event, and one worker-restart validation operation.
- Optional broader check:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "validation_salvage or preserved_active_validation_slot_exhaustion"`
  - Passes without weakening stale-failure behavior.
