# Remonitor Operator State Validation

Plan reference: `plans/REMONITOR_OPERATOR_STATE_PLAN.md`

## Requirement Status

- Operator action requests must capture the workspace they were issued for:
  Complete. `runWorkspaceOperatorAction` now captures `workspaceId` before
  submitting the request and uses it for the idempotency key and API path.
- Operator action success or error state must not be written if that workspace
  is no longer selected when the response arrives:
  Complete. Operator action success and error handlers check
  `selectedIdRef.current !== workspaceId` before writing action state.
- Workspace-scoped state should be cleared in the same selection update batch
  where practical so old success text and warnings are not rendered for the next
  selected workspace:
  Complete. Selection changes now clear detail, stream, retry, and operator
  state in `useLayoutEffect`, before the changed selection paints.
- Stale workspace detail loads from an older selection should not overwrite the
  current selected workspace detail:
  Complete. `loadWorkspace` returns without updating detail if the selected
  workspace ref no longer matches the load target.
- Add focused regression coverage for the selection guard:
  Complete. `apps/console/lib/console-dashboard-source.test.mjs` now asserts the
  selected-workspace ref, captured operator workspace ID, and stale-response
  guard are present.

## Evidence

Files changed:

- `apps/console/components/console-dashboard.tsx`
- `apps/console/lib/console-dashboard-source.test.mjs`
- `plans/REMONITOR_OPERATOR_STATE_PLAN.md`
- `plans/REMONITOR_OPERATOR_STATE_VALIDATION.md`

Focused checks:

- Before implementation, the new regression failed with the expected missing
  `selectedIdRef` assertion.
- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON apps/console/lib/console-dashboard-source.test.mjs --test-name-pattern "operator action state"`
  passed after implementation.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this agent phase.
