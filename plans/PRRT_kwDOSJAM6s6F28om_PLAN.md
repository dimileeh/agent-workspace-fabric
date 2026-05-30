# PRRT_kwDOSJAM6s6F28om Plan

## Problem Statement

`interworkspace_owned_paths` only recognizes AWF planning artifacts under the
default `docs/awf-plans` root. Profiles may configure different planning
artifact paths, such as `.awf/plans/{workspace_id}.md` or
`docs/alternate/{workspace_id}.json`, and those generated paths/scopes should
not create inter-workspace merge queue or overlap blockers.

## Scope

- Derive additional internal planning artifact paths and scopes from a
  workspace's resolved profile planning configuration.
- Preserve the existing default `docs/awf-plans` behavior for legacy callers
  that do not have profile context.
- Keep real repository paths such as README files blocking unless the path is a
  generated artifact file or an explicit configured artifact scope.
- Add focused regressions for custom planning roots in the shared helper,
  active workspace overlap checks, and merge queue ordering.

## Requirements Checklist

- Custom profile planning scopes such as `docs/alternate/**` are filtered from
  inter-workspace owned-path comparisons when the resolved profile uses
  `docs/alternate/{workspace_id}` artifact templates.
- Custom generated artifact filenames such as `docs/alternate/ws_123.md` and
  `docs/alternate/ws_123.json` are recognized as internal when profile context
  is supplied.
- Real files under the same custom root, such as `docs/alternate/README.md`,
  remain ordinary owned paths.
- Existing default `docs/awf-plans` behavior and normalization guarantees stay
  intact.
- Focused tests and lint cover the touched helper and affected behavior.

## Implementation Steps

1. Add failing helper and merge-safety regressions for custom profile planning
   artifact paths.
2. Extend the common owned-path helper to derive internal artifact paths and
   scopes from resolved profile planning templates.
3. Thread the derived artifact paths through active overlap, merge queue,
   lock-risk, overlap graph, and staleness call sites that have workspace
   profile context.
4. Re-run the focused tests and lint for the touched files only.

## Verification

- Run targeted pytest nodes for the new common helper regression.
- Run targeted pytest nodes for custom active overlap and merge queue ordering.
- Run focused `ruff check` on changed Python files.
- Do not run full AWF/GitHub validation; AWF owns broad validation and merge
  gating after the agent phase.
