# Workspace Inspector Implementation Plan

## Intended files/modules to touch
1. **`apps/console/components/console-dashboard.tsx`**:
   - Refactor layout to introduce a right-side `WorkspaceInspector` drawer for selected workspace details.
   - Move the workspace-specific panels (`WorkspaceSummary`, `LifecycleRail`, `RuntimePanel`, etc.) from the bottom of the main section into the new Inspector component.
   - Update `selectedId` state management to optionally sync with URL search params (e.g., `?workspaceId=...`) for "selected workspace persistence", or use `sessionStorage` if strictly UI-state. URL query string is preferred for deep linking.
   - Pass a "ghost" or "transparent" variant to inner panels to fulfill the "no cards inside cards" requirement.
2. **`apps/console/components/workspace-inspector.tsx`** (New):
   - Create a dismissible, embedded inspector drawer component.
   - Use `position: absolute` or `fixed` sliding drawer to ensure opening it does not reflow or "jump" the global dashboard panes.
   - Include a close control (`X` button) that clears the selection.
3. **`apps/console/tests/dashboard-inspector.spec.ts`** (New):
   - Add Playwright E2E tests for the new inspector behavior.

## Tests to write first
- **`dashboard-inspector.spec.ts`**:
  - **Open/Close**: Click a workspace row in the sidebar -> Inspector becomes visible with workspace details; click "Close" in the inspector -> Inspector is hidden, and global view is fully restored.
  - **Selected Workspace Persistence**: Verify that selecting a workspace updates the URL search params (or local storage), and reloading the page automatically opens the inspector for that workspace.
  - **Responsive Layout**: Verify the inspector renders as a full-width overlay on mobile and a fixed-width side drawer on desktop.
  - **No Jump**: Validate that the bounding box of global dashboard components (e.g., `ResourceCapacityPanel`) does not change width/height when the inspector is toggled.

## Validation commands
- `npm run lint` (inside `apps/console`)
- `npm run check` (TypeScript verification)
- `npx playwright test tests/dashboard-inspector.spec.ts` (Verify new inspector tests)

## Risks, assumptions, and explicit non-goals
- **Assumption:** "Without causing dashboard panes... to jump" means the inspector must not trigger a CSS layout reflow of the main dashboard. A fixed-position sliding right drawer (overlay) is the standard and safest approach to meet this requirement.
- **Assumption:** "No cards inside cards" implies we need to remove the outer borders and background colors from the individual `Panel` components when they are rendered inside the already-bordered Inspector drawer. We will extend the existing `Panel` component with a visual variant (e.g., `variant="ghost"`).
- **Assumption:** "Selected workspace persistence" means URL persistence (`?workspaceId=...`), allowing users to share links directly to a workspace's details.
- **Explicit Non-goal:** We will not make any changes to the API schemas or data fetching logic, keeping the layout changes strictly component-scoped as required by the coordination warning.
- **Explicit Non-goal:** We will not redesign the internal content of the panels (logs, events, metrics), only their container styling to remove nested card visuals.
