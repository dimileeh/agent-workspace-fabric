# PR 329 CI Fix Plan

## Goal

Repair the focused `python-full-coverage` CI failures without weakening the
maintainability guards or running broad AWF-owned validation locally.

## Steps

1. Update the remonitor idempotency regression to assert the newly persisted
   `pending_operator_hint` audit payload for operator reasons.
2. Split oversized first-party modules and tests along existing package
   boundaries so every first-party source file stays under 1500 lines.
3. Replace the `pr_monitor_runner` public facade's dynamic `__getattr__` export
   with explicit imports compatible with the facade guard.
4. Run the provided focused pytest repro and any narrow follow-up checks needed
   for moved tests/imports.
5. Record results in `plans/PR_329_CI_FIX_VALIDATION.md`, noting that broad
   AWF/GitHub validation remains owned by AWF after agent completion.
