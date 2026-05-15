# Review Thread PRRT_kwDOSJAM6s6CNQ1K Plan

## Problem Statement And Scope

PR review feedback reports that callback delivery validates all DNS answers for a
callback target, but then pins delivery to only the first validated address. If
the first public address is unreachable and a later validated address is
reachable, every retry can fail despite a safe usable address being available.

Scope is limited to callback target validation and delivery address fallback in
`src/awf/service/callbacks.py`, with focused regression coverage in
`tests/unit/service/test_callbacks.py`.

## Requirements Checklist

- Preserve SSRF safety: every resolved callback address must still be public
  before any delivery attempt is made.
- Preserve the validated address set rather than collapsing it to the first
  address.
- Try later validated callback addresses when delivery to an earlier validated
  address raises a request exception.
- Do not retry alternate addresses after an HTTP response is received; HTTP
  status handling remains the existing success/retry policy.
- Add a regression test for a multi-address callback target where the first
  validated address fails and the second succeeds.

## Implementation Steps

1. Update the validated target shape to carry a tuple of connect IP addresses.
2. Update DNS validation to return the complete validated tuple after checking
   every resolved address is public.
3. Add a small delivery helper that invokes the existing poster once per
   validated address until it receives a response, re-raising the last exception
   if all addresses fail.
4. Wire `CallbackDeliveryService.drain_due` through the helper.
5. Add the focused regression test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  must pass.
