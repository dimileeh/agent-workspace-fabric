# PRRT_kwDOSJAM6s6CN_6c Plan

## Problem Statement and Scope

The review thread reports that callback delivery DNS validation treats NAT64
translation-prefix IPv6 addresses as public even when they encode internal IPv4
targets such as `169.254.169.254`. The fix is scoped to callback target
publicness checks used during delivery-time DNS validation, with focused
regression tests.

## Requirements Checklist

- Add a regression test proving a resolved NAT64 address that embeds a
  non-public IPv4 address is rejected before any callback POST is attempted.
- Preserve the existing callback behavior for ordinary public IPv4 and IPv6
  targets.
- Keep the change localized to callback target validation code.
- Validate with the narrowest relevant test command.
- Record implementation validation in a matching validation document.

## Implementation Steps

1. Add a unit regression in `tests/unit/service/test_callbacks.py` for a
   callback target resolving to a NAT64 translation address encoding
   `169.254.169.254`.
2. Confirm the focused regression fails against the current implementation when
   practical.
3. Update `src/awf/service/callbacks.py` so `_is_public_ip` rejects recognized
   NAT64 translation addresses when their embedded IPv4 address is not public.
4. Re-run the focused callback service tests.
5. Write `plans/PRRT_kwDOSJAM6s6CN_6c_VALIDATION.md` with requirement evidence.
