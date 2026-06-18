# Validation — protected-file pause: CONSOLE + hardening slice (WS-3 of 3)

Plan: `docs/awf-plans/ws_fd877d96989f4218a517a679.md`
Workspace: `ws_fd877d96989f4218a517a679`

## What shipped (console-only surfacing; no backend behaviour changed)

1. **Status rendering** — `lib/format.ts`: `blocked` added to `lifecycleStages`
   (before `monitoring_pr`, non-terminal), `statusTone("blocked") → "warn"`,
   `statusGlyph("blocked") → "⏸"` (a shape used nowhere else, so the pause state is
   never color-only). `lib/types.ts`: `"blocked"` added to the `WorkspaceStatus`
   union; `blocked: number` added to `WorkspaceSaturationCounts`; new
   `WorkspaceBlockViolation` / `WorkspaceBlockState` interfaces mirroring
   `api/schemas.py`; `block_state?` added to `Workspace`. Saturation fallback mapper
   (`console-dashboard-shared.tsx`) carries `blocked`.
2. **Fleet KPI** — `console-dashboard.tsx` `fleetKpis`: new **"Awaiting operator"**
   KPI = `counts.blocked ?? 0` (warn tone), placed after "Monitoring PR". The
   **Running** KPI expression is untouched (`running + validating + pushing`, the
   PR #598 contract); `blocked` lands in **Active** server-side via `active_total`.
   `FleetHealthStrip` grid widened `xl:grid-cols-8 → xl:grid-cols-9`.
3. **Blocked-age** — `lib/blocked-format.ts` (new pure helpers): `blockedSince`
   (list proxy: `last_event.occurred_at` when `new_state==="blocked"`, else
   `updated_at`), `blockedAgeSeconds` (deterministic, injectable clock). The list
   renders a "Blocked for N" badge; the inspector uses the precise
   `block_state.blocked_at`.
4. **Inspector** — `console-dashboard-workspace-detail.tsx` `BlockedViolationBlock`:
   for a `blocked` workspace, shows reason code / block type / resume phase /
   blocked-at, the violation list (path · pattern · section · reason), and the two
   copy-able guide commands from `formatBlockedResolutionCommands`
   (`--grant '<path>' --reason '<why>'` and `--directive 'revert <path>; <alternative>'`).
   Degrades to a "details loading/unavailable" line when `block_state` is absent
   (overview-only render).

## Tests added/extended (TDD — written before implementation)

- `lib/format.test.mjs` — `blocked` tone/glyph/lifecycle.
- `lib/blocked-format.test.mjs` (new) — `blockedSince`, `blockedAgeSeconds`,
  `formatBlockedResolutionCommands` (incl. null/empty-path placeholder).
- `tests/dashboard-blocked.spec.ts` (new) — blocked badge (⏸) + "Blocked for"
  indicator; "Awaiting operator" KPI counts blocked, in Active but NOT Running;
  inspector shows the violation path + both guide commands.
- `tests/dashboard-running-kpi.spec.ts` — added `blocked` to the fixture; asserts
  Running stays `running+validating+pushing` while Active grows.
- `tests/theme-accessibility.spec.ts` — the mobile inspector-open step now scrolls
  the first card into view before its raw-coordinate click; the required 9th KPI
  ("Awaiting operator") lengthens the mobile fleet strip enough to push the first
  card below the fold otherwise. Pure test-robustness change; no app behaviour.

## Focused checks run locally (AWF/CI owns the broad gates)

- `npm --prefix apps/console run test` → 191 pass / 0 fail.
- `npm --prefix apps/console run typecheck` → clean.
- `npm --prefix apps/console run lint` → clean.
- `npx playwright test` (full console browser suite) → 48 pass / 0 fail.

Per the workspace contract, the full Python suite, coverage gate, OpenAPI drift
gate and console CI job are owned by AWF + GitHub CI after agent completion; they
were not run here.

## Item 5 — hardening check-first matrix (no duplicate tests added)

The backend authorization-abuse surface is already covered by WS-1/WS-2; this slice
reads its fields only and adds **no** backend tests (avoiding the protected-file
trap and keeping the diff console-scoped). Verified existing coverage:

- **Grant `../` traversal / absolute / empty rejected** — `_canonicalize_grant_path`
  (`controls_guide.py:373-386`) + `test_guide_blocked_rejects_unsafe_grant_path`,
  `test_canonicalize_grant_path_strips_leading_dot_slash`,
  `test_canonicalize_grant_path_rejects_empty_after_normalization`.
- **Directory-wide grant** — `test_directory_grant_suppresses_nested_weakening_workflow`,
  `test_directory_grant_without_ack_does_not_suppress`,
  `test_wildcard_grant_does_not_authorize_non_matching_workflow`
  (`test_quality_gates_parts/test_quality_gates_grants.py`).
- **Grants rejected on non-blocked / `monitoring_pr`** —
  `test_guide_grants_rejected_on_monitoring_pr`.
- **Operator-identity binding (no agent self-grant)** — `operator` is set by the
  control plane, never from workspace input (`controls_guide.py:406-407`
  docstring; route passes `payload.operator` from the operator API call, not the
  agent). Enforced by `test_guide_blocked_grant_same_key_different_operator_conflicts`
  and `test_guide_blocked_grant_same_operator_whitespace_replays`; a grant also
  requires a reason (`test_guide_blocked_grant_requires_reason`) and the
  policy-downgrade ack (`test_guide_blocked_weakening_grant_requires_ack`).
- **Symlinked protected file** — not a reachable vector at this layer. The quality
  gate classifies **git-diff path strings**: a tracked symlink appears in the diff
  as its own path (a symlink blob), and content edited *through* a symlink shows up
  in the diff as a change to the real protected path, which the pattern matcher
  flags. There is no string-level seam where a symlink stands in for a protected
  path, so no focused test is warranted; `_canonicalize_grant_path` independently
  fails closed on `..`/absolute paths.
- **Egress to the guide/grant endpoint** — the guide control lives behind the
  operator REST/MCP surface; the agent runtime runs unprivileged with the profile's
  restricted egress posture and holds no control-plane API token, so it cannot
  reach `POST /v1/workspaces/{id}/controls/guide`. This is owned by the
  profile/network-posture layer (WS-1/WS-2) and unchanged here.
