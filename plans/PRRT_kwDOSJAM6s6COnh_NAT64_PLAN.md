# PRRT_kwDOSJAM6s6COnh NAT64 Callback Target Plan

## Problem Statement And Scope

Address unresolved review thread `PRRT_kwDOSJAM6s6COnh_` on
`src/awf/common/callback_targets.py`. The callback target validator recognizes
the local-use NAT64 prefix `64:ff9b:1::/48`, but decodes it by reading the low
32 bits as if every NAT64 prefix were `/96`. That can treat a local-use NAT64
address with an embedded private IPv4 target as public when the suffix happens
to contain public-looking low bits.

Scope is limited to shared callback target IP publicness policy and regression
coverage for local-use NAT64 decoding.

## Requirements Checklist

- [ ] Decode `64:ff9b::/96` callback targets exactly as before.
- [ ] Decode `64:ff9b:1::/48` callback targets using the RFC 6052 embedded
  IPv4 layout, ignoring suffix bits instead of treating them as the IPv4
  address.
- [ ] Reject local-use NAT64 targets whose embedded IPv4 address is private,
  even when suffix bits look public.
- [ ] Preserve public local-use NAT64 targets whose embedded IPv4 address is
  public.
- [ ] Run focused regression tests and lint for the touched files.
- [ ] Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Update the shared callback target policy test with RFC 6052 `/48` local-use
   NAT64 examples that fail against the current low-32-bit decoder.
2. Confirm the focused common callback target test fails before the code change.
3. Add a small helper in `src/awf/common/callback_targets.py` that extracts the
   embedded IPv4 address according to the matched NAT64 prefix length.
4. Extend the delivery-time NAT64 regression to cover a local-use `/48` address
   that embeds private IPv4 with public-looking suffix bits.
5. Re-run the focused test, then run relevant callback target tests and ruff on
   touched files.
6. Record implementation evidence in
   `plans/PRRT_kwDOSJAM6s6COnh_NAT64_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  must pass after the fix, and the updated local-use NAT64 regression must fail
  before the implementation change when practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py`
  must pass.
