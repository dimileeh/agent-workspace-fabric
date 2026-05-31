# PRRT_kwDOSJAM6s6F8RTZ Console Remonitor Warnings Plan

## Problem Statement and Scope

Backend control responses now include `warnings`, including remonitor warnings
that explain when auto-merge is paused or frozen after reviewer-settle has
elapsed. The console remonitor flow still types `WorkspaceControlResponse`
without warnings and stores only the operation id/status/message in the success
state, so the operator-facing UI drops the warning payload.

Scope is limited to the console client response type, success-summary
propagation, and rendering of control response warnings. Backend behavior is
already covered by existing schemas and service tests.

## Requirements Checklist

- Add a focused regression proving control success summaries retain backend
  warnings.
- Add a focused regression proving the operator controls UI has a render path
  for success warnings.
- Update the console API type contract so `WorkspaceControlResponse` includes
  `warnings` entries with `warning_code` and `message`.
- Carry warning entries from `summarizeWorkspaceOperatorSuccess` into operator
  action state and render them where operators see successful control results.
- Keep validation focused; full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Update console unit/source tests to describe the expected warning propagation
   and UI render path, then confirm the focused tests fail when practical.
2. Add `WorkspaceControlWarning` and `warnings` to the console types and type
   contract.
3. Extend `WorkspaceOperatorSuccessSummary` to include warnings from
   `WorkspaceControlResponse`, with operation responses preserving an empty
   warning list.
4. Store success warnings in `OperatorActionState` and render warning messages
   below the operator success summary.
5. Run focused console tests and targeted type checking for the touched files.

## Verification Commands and Pass Criteria

- `cd apps/console && node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/workspace-operator-controls.test.mjs lib/console-dashboard-source.test.mjs`
  - Fails before implementation for the new warning regressions and passes
    after implementation.
- `cd apps/console && npm exec tsc -- --noEmit --pretty false --strict --target ES2017 --lib dom,dom.iterable,esnext --module esnext --moduleResolution bundler --allowImportingTsExtensions --jsx react-jsx --skipLibCheck lib/types-contract.test.ts`
  - Passes after the type contract is updated.
- `git diff --check`
  - Passes after edits.
- Broad AWF/GitHub validation, full frontend builds, and coverage gates are
  intentionally left to AWF after agent completion.
