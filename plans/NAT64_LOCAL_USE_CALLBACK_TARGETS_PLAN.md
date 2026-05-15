# NAT64 Local-Use Callback Targets Plan

## Problem Statement and Scope

An unresolved PR review thread reports that callback target validation extracts
embedded IPv4 addresses from the full `64:ff9b:1::/48` locally-assigned NAT64
namespace as if it were a single RFC 6052 prefix. Operator deployments can carve
working `/96` sub-prefixes from that namespace, so prefix-length-based
extraction can misclassify private IPv4 callback destinations as public.

Scope is limited to callback target publicness validation and focused
regression tests.

## Requirements Checklist

- Block callback target IPs in `64:ff9b:1::/48` unconditionally.
- Preserve existing well-known `64:ff9b::/96` NAT64 embedded IPv4 checks.
- Add a regression for the reported `64:ff9b:1:c001::c0a8:0101` bypass.
- Keep the change focused and avoid unrelated callback delivery behavior.
- Validate with focused tests for callback target policy and delivery rejection.

## Implementation Steps

1. Update focused tests to expect unconditional rejection for
   `64:ff9b:1::/48`, including the review's `/96` sub-prefix example.
2. Run the focused tests to confirm the new regression fails against current
   behavior.
3. Move the local-use NAT64 namespace from translation extraction to the
   blocked-address policy.
4. Re-run focused tests and the relevant common test module.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  - Passes after implementation.
