# Setup Retry Counter Separation Plan

## Problem Statement and Scope

CodeRabbit comment `4293039604` reports that setup dependency network retries and
generic flaky validation retries share one counter in `ValidationRunner._run_commands`.
That can make a command exhaust the generic retry budget early after a setup network
retry.

Scope is limited to validation retry accounting and focused regression coverage.

## Requirements Checklist

- Add a regression test proving a setup dependency network retry does not consume the
  generic flaky retry budget.
- Track setup dependency retry count separately from generic flaky retry count.
- Keep setup retry logging, backoff, attempt metadata, and exhaustion checks based on
  the setup-specific counter.
- Report final command retry counts as the combined retry total while preserving
  existing setup metadata behavior.
- Validate with the narrow unit test surface for runtime validation.

## Implementation Steps

1. Add a runtime validation test that queues setup network failure, flaky timeout,
   then success with `validation.retry_budget=1`.
2. Split the `_run_commands` retry counters into setup-specific and generic flaky
   counters.
3. Use the combined retry count for final `ValidationCommandResult.retry_count` and
   setup recovery metadata; use combined retry budget when setup metadata reports a
   command that used both retry mechanisms.
4. Run the new focused test, then the relevant runtime validation test module.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  must pass.
