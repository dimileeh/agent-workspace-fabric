# PRRT_kwDOSJAM6s6CZofM Database URL Credentials Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CZofM` reports that production guardrail
database URL detection eagerly reads `SplitResult.port`. That makes malformed
ports raise a raw `ValueError` even when the URL uses non-default production
credentials, while the guardrail only needs to reject AWF's bundled local
credentials.

Scope is limited to `src/awf/common/config.py`, the focused production
configuration tests, and this plan/validation record.

## Requirements Checklist

- Add a regression proving production guardrails do not raise a raw port-parsing
  `ValueError` for non-default database credentials with a malformed port.
- Preserve structured rejection of bundled AWF local database credentials in
  production.
- Avoid broad URL validation or unrelated config refactors.
- Run the narrow focused tests that prove the review thread behavior.
- Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Add or update focused tests in `tests/unit/service/test_config.py` for the
   malformed-port/non-default-credentials path and the default-credentials path.
2. Run the focused tests before implementation to confirm the new regression
   fails on the current helper.
3. Simplify `_is_default_local_database_url_or_credentials` so it checks only
   normalized emptiness, exact default URL, and decoded username/password.
4. Re-run the focused tests and a narrow config unit test file.
5. Record validation results in the matching validation file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`

Pass criteria: all focused config tests pass, with the malformed non-default
port path producing no raw `ValueError` from the production guardrail.
