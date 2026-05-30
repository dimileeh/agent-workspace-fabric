# PRRT_kwDOSJAM6s6F3EWW Custom Plan Parent Scope Plan

## Problem Statement And Scope

The PR review reports that custom planning artifact templates such as
`docs/runbooks/{workspace_id}.md` currently add the whole parent scope
`docs/runbooks/**` to the internal plan artifact set. That makes a workspace
that owns the real directory scope lose interworkspace overlap/stale detection
for non-generated files under that directory.

Scope is limited to `src/awf/common/owned_paths.py` and focused unit coverage in
`tests/unit/common/test_owned_paths.py`.

## Requirements Checklist

- Preserve real repository directory scopes such as `docs/runbooks/**` when a
  custom planning artifact is only a workspace-id filename in that directory.
- Continue filtering exact generated custom artifact filenames and workspace-id
  filename globs.
- Continue supporting directory-scope artifact filtering when the template's
  parent directory itself is workspace-specific, such as
  `docs/plans/{workspace_id}/plan.md`.
- Keep the default `docs/awf-plans/**` internal artifact behavior unchanged.
- Run only focused validation for the changed owned-path helper.

## Implementation Steps

1. Add a regression test for `docs/runbooks/{workspace_id}.md` proving
   `docs/runbooks/**` remains an interworkspace owned path while rendered and
   globbed artifact filenames are filtered.
2. Add coverage for a workspace-specific parent directory template to preserve
   intended directory artifact behavior.
3. Narrow `_internal_plan_artifact_paths_from_template` so parent `/**` entries
   are emitted only when the parent contains `{workspace_id}`.
4. Run focused unit tests for `tests/unit/common/test_owned_paths.py`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`

Full AWF/GitHub validation is managed after agent completion by AWF and CI.
