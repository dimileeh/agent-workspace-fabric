# Review Comment 4454403868 Timeout And DNS Validation

Plan reference: `review_comment_4454403868_timeout_dns_PLAN.md`

## Requirement Status

- Add a regression test proving timeout-budget exhaustion after an attempted
  address raises `TimeoutError`, not the prior connection exception:
  Complete. Added
  `test_validated_address_timeout_after_failure_raises_timeout_with_prior_failure_cause`.
- Preserve attempted-address failure details as causal traceback evidence when
  the final delivery signal is timeout:
  Complete. Timeout errors raised after prior address failures now chain an
  `ExceptionGroup` cause that includes the attempted addresses and exception
  types.
- Preserve aggregate per-address failure reporting when all attempted validated
  addresses fail before the timeout budget is exhausted:
  Complete. The existing all-address-failed `ExceptionGroup` path remains for
  non-timeout exhaustion, and the callback service test module passes.
- Move callback target validation off asyncio's shared default thread pool and
  into a callback-specific bounded executor:
  Complete. `_validate_callback_target_with_timeout` now calls
  `_run_callback_target_validation`, which uses a four-worker callback-specific
  `ThreadPoolExecutor`.
- Keep the existing delivery timeout around the whole validation call:
  Complete. The existing `asyncio.wait_for(..., timeout=timeout)` wrapper is
  preserved, and the validation-time budget accounting regression still passes.
- Avoid unsafe process-global DNS timeout changes:
  Complete. No `socket.setdefaulttimeout` or resolver-global mutation was added;
  `getaddrinfo` remains bounded by AWF's async delivery timeout while any
  uninterruptible resolver work is confined to the callback-specific executor.
- Do not push, switch branches, or write any GitHub comment:
  Complete. No branch switch, push, or GitHub write was performed.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/review_comment_4454403868_timeout_dns_PLAN.md`
- `plans/review_comment_4454403868_timeout_dns_VALIDATION.md`

Regression-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'timeout_after_failure or fallback_stops_when_timeout_budget_is_exhausted or offloads_callback_target_validation or counts_callback_target_validation'`
  - Failed before implementation: four focused failures covering masked timeout
    signaling, timeout-vs-aggregate precedence, continued `asyncio.to_thread`
    usage, and validation budget accounting through the new helper.
  - Passed after implementation: `4 passed, 24 deselected`.

Verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: `28 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: `Success: no issues found in 155 source files`.

## Gaps

None.
