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

## Iteration 2: Exact Coverage Failure

### Problem statement and scope

GitHub Actions run `26708986644` failed only in `python-full-coverage`: all
tests passed, but combined coverage was `98.92%` against the required `99%`.
The missed opportunities are concentrated in the new operator hint monitor
runner paths and adjacent decision branches.

### Requirements checklist

- Add focused tests that cover real operator-hint runner behavior; do not
  weaken or skip coverage gates.
- Avoid protected workflow, quality-gate, and broad configuration edits.
- Keep tests below the repository's first-party line-count guardrails.
- Run narrow pytest/ruff checks for the new tests and touched files only.
- Record focused evidence here and leave full AWF/GitHub coverage validation to
  AWF after the agent phase.

### Implementation steps

1. Add a small operator-hint coverage test module for early-return, default
   verdict reason, non-policy push failure, and pushed-with-empty-head paths.
2. Prefer existing runner fixtures and helper types over new test scaffolding.
3. Run the new focused tests, then targeted lint for touched files.
4. Update `plans/PR_329_CI_FIX_VALIDATION.md` with command results.
