# PRRT_kwDOSJAM6s6CMQkz Readiness Reason Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CMQkz` reports that
`render_core_readiness_pretty` prints a `reason:` sub-line for every Core
release readiness check, including passing `ok` checks. For all-green output
this adds noise such as `SERVICE_STATUS_OK` without adding diagnostic value.

Scope is limited to the pretty text renderer for Core release readiness.
Structured JSON output must continue to include `reason_code`.

## Requirements Checklist

- Add a regression test proving pretty output omits `reason:` for `ok` checks.
- Preserve `reason:` output for non-`ok` checks.
- Preserve evidence rendering for checks whose reason line is omitted.
- Keep JSON serialization and readiness collection behavior unchanged.
- Run focused validation for the changed behavior.

## Implementation Steps

1. Add a unit test around `render_core_readiness_pretty` with one `ok` check and
   one `fail` check.
2. Confirm the test fails before implementation.
3. Update `render_core_readiness_pretty` to append `reason:` only when the check
   status is not `ok`.
4. Run the focused readiness test module or targeted test.
5. Record validation results in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py -q`

Pass criteria: the readiness unit test module passes, including the new
regression test.
