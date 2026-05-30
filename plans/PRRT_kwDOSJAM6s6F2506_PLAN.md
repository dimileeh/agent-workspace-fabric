# PRRT_kwDOSJAM6s6F2506 Plan

## Problem Statement

The owned-path inter-workspace filter still keeps the shared
`docs/awf-plans/**` planning artifact glob. AWF persists that glob for
workspace planning/conformance artifacts, so unrelated workspaces can appear to
overlap in merge queue and active-overlap checks solely because they carry the
same AWF-generated artifact scope.

## Scope

- Treat the normalized `docs/awf-plans/**` glob as an internal plan-artifact
  owned-path scope for inter-workspace dependency checks.
- Preserve filtering for generated `ws_*` plan/conformance artifact filenames
  and globs.
- Preserve ordinary ownership for concrete tracked docs such as
  `docs/awf-plans/README.md` when declared directly.
- Keep target-branch staleness behavior intact: generated plan artifacts remain
  advisory, while README changes remain blocking when matched by a workspace's
  declared scope.

## Requirements Checklist

- `docs/awf-plans/**` is classified as an internal plan-artifact owned path.
- `interworkspace_owned_paths()` removes `docs/awf-plans/**`.
- Merge queue blockers ignore candidates that share only `docs/awf-plans/**`.
- Active overlap lookup and overlap graph ignore `docs/awf-plans/**`-only
  matches.
- Direct README ownership remains ordinary and overlapping when both sides
  declare `docs/awf-plans/README.md`.

## Implementation Steps

1. Update focused owned-path tests to cover the shared glob as internal while
   preserving direct README ownership.
2. Update merge-queue, repository overlap, and overlap-graph regressions for
   broad shared plan-artifact scopes.
3. Extend the owned-path classifier to recognize `docs/awf-plans/**`.
4. Run focused tests and lint for the touched helper and affected behavior.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- Focused pytest nodes for the affected merge queue, repository overlap, and
  overlap graph regressions.
- `uv run --python 3.12 --extra dev ruff check` on touched Python files.
- Full AWF/GitHub validation is intentionally not run inside the agent phase;
  AWF owns broad validation and merge gating after completion.
