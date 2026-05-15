# Comment 4454303511 Setup Retry Metadata Plan

## Problem Statement and Scope

PR review comment `issue:4454303511` includes two concerns:

- The executor setup dependency event recorder must not fail an otherwise
  runnable workspace if event persistence has a transient DB error.
- Setup dependency network metadata currently reports `retry_count` as the
  combined setup-network plus generic flaky retry count, while `attempts`
  contains only setup dependency network attempts.

The executor concern is already handled in this branch by a local `try/except`
around `_record_setup_dependency_network_events`, so this implementation scope is
limited to clarifying runtime validation metadata and preserving the existing
executor fix.

## Requirements Checklist

- Preserve command-level `ValidationCommandResult.retry_count` as the total
  number of retries for the command.
- Report setup dependency metadata `retry_count` as setup dependency network
  retries so it matches the setup-only `attempts` list.
- Add explicit setup, flaky, and total retry counters to the setup dependency
  metadata for observer clarity.
- Add or update a regression test covering one setup-network retry, one flaky
  retry, and final success.
- Validate the focused runtime validation surface.

## Implementation Steps

1. Update the mixed retry regression assertion first so the current code fails
   by showing metadata `retry_count` still reports the total retry count.
2. Change `_with_setup_dependency_network_metadata` to accept separate setup,
   flaky, and total retry counts.
3. Update all setup metadata call sites in `ValidationRunner._run_commands`.
4. Run the focused mixed retry test, then the relevant validation unit module.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_does_not_consume_flaky_retry_budget -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
