# CI Monitor Runner Failure Plan

## Problem statement and scope
PR #353 fails the `python-full-coverage` CI job on focused PR monitor runner and monitor action logging tests. The fix is scoped to the behavior exercised by the reported failing node IDs: transient status-fetch handling, merge action logging payloads, compose teardown behavior after merge completion, and deferred thread capture/merge flows if reproduced.

## Requirements checklist
- Reproduce the AWF-provided focused failures before editing.
- Identify the root cause in monitor runner/action logging code without changing protected workflow or gate files.
- Add or update regression coverage only where needed by the focused failure surface.
- Preserve real check behavior; do not skip, disable, or weaken tests.
- Run focused verification for the affected tests only; broad AWF/GitHub validation remains managed by AWF after agent completion.
- Commit the fix locally on the current AWF-managed branch without pushing.

## Implementation steps
1. Run the AWF-provided focused pytest command and inspect assertion diffs.
2. Read the failing tests and adjacent monitor runner/logging implementation.
3. Make the smallest behavior change that satisfies the intended monitor lifecycle/logging semantics.
4. Re-run the initially failing focused command, then any additional named failing node IDs needed to cover the same root cause.
5. Record validation evidence in `plans/CI_MONITOR_RUNNER_FAILURE_VALIDATION.md`.
6. Commit the plan, validation, tests, and implementation changes locally.

## Verification commands and pass criteria
- `uv run --python 3.12 --extra dev pytest <AWF-provided focused node IDs> -q` passes.
- Additional targeted pytest node IDs from the CI evidence pass if they share the same changed path.
- No broad coverage, full test suite, full frontend build, or CI-equivalent command is run locally.
