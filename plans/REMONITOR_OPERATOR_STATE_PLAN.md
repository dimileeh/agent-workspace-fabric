# Remonitor Operator State Plan

## Problem Statement And Scope

The PR review thread reports that operator control success state and
`REMONITOR_PAST_SETTLE` warnings can remain visible after switching from one
workspace to another. The likely unsafe path is an in-flight operator action for
workspace A completing after the operator has selected workspace B, writing A's
success or warning state into the shared console state.

Scope is limited to the AWF console dashboard state flow and focused regression
coverage. No broad AWF or GitHub-owned validation suite will be run in the agent
phase.

## Requirements Checklist

- Operator action requests must capture the workspace they were issued for.
- Operator action success or error state must not be written if that workspace
  is no longer selected when the response arrives.
- Workspace-scoped state should be cleared in the same selection update batch
  where practical so old success text and warnings are not rendered for the next
  selected workspace.
- Stale workspace detail loads from an older selection should not overwrite the
  current selected workspace detail.
- Add focused regression coverage for the selection guard.

## Implementation Steps

1. Add a focused regression test in the existing console dashboard source test
   file and confirm it fails before the implementation.
2. Track the latest selected workspace in a ref.
3. Clear workspace-scoped detail, retry, log, and operator action state during
   selected workspace changes.
4. Update `runWorkspaceOperatorAction` to use a captured `workspaceId` and skip
   state writes when `selectedIdRef.current` no longer matches it.
5. Guard `loadWorkspace` so a stale load cannot update detail after selection
   changes.
6. Run the focused console source test.

## Verification Commands And Pass Criteria

- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/console-dashboard-source.test.mjs --test-name-pattern "operator action state"`
  - Passes after the implementation.
  - This is a focused Node test for the changed console dashboard behavior.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
