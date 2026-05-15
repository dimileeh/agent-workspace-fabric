# Callback Address Timeout Plan

## Problem Statement and Scope

The callback delivery fallback loop tries each validated callback address with the full subscription timeout. If earlier resolved addresses black-hole connections, a single delivery can consume the timeout once per address. Scope is limited to bounding one delivery attempt's address fallback loop to one timeout budget.

## Requirements Checklist

- Add a regression test proving fallback attempts receive only the remaining timeout budget.
- Keep successful fallback across validated addresses working when prior addresses fail quickly.
- Preserve current failure logging and exception notes for attempted addresses.
- Avoid changing callback registration, target validation, or repository behavior.

## Implementation Steps

1. Add a unit test in `tests/unit/service/test_callbacks.py` around `_post_to_validated_callback_addresses` with a controlled monotonic clock.
2. Update `_post_to_validated_callback_addresses` to compute one deadline and pass the remaining budget to each poster attempt.
3. Stop trying additional addresses once the delivery budget is exhausted and re-raise collected failures as before.
4. Run the focused callback tests and relevant lint.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`

Both commands must pass.
