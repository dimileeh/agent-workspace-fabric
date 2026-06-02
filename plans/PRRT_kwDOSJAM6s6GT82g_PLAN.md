# PRRT_kwDOSJAM6s6GT82g Plan

## Problem Statement And Scope

An unresolved review thread reports that retrying a legacy failed or cancelled
workspace with no `ResourceReservation` and no `resolved_profile` loses inline
DinD demand when `requested_profile.docker.mode` is `dind`. The retry path must
preserve that demand so the new authoritative retry reservation has
`dind_slots=1`.

Scope is limited to the no-source-reservation retry fallback in
`src/awf/service/workspaces_retry.py` and focused regression coverage.

## Requirements Checklist

- Reproduce the legacy retry shape with no source reservation, no
  `resolved_profile`, and inline `requested_profile` requesting DinD.
- Preserve DinD demand when creating the retry reservation for that legacy
  shape.
- Keep existing behavior for retry sources that already have a reservation or a
  resolved profile.
- Run focused validation only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a regression test adjacent to existing retry DinD reservation coverage.
2. Update the retry fallback to derive DinD mode from `resolved_profile` first,
   then `requested_profile` when no resolved snapshot exists.
3. Run the focused regression test and a narrow retry test selection.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GT82g_VALIDATION.md`.
