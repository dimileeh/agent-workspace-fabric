# PRRT_kwDOSJAM6s6F3HjC Requested Overlap Plan

## Problem Statement And Scope

The review thread reports that active owned-path overlap lookup filters requested
paths with custom planning artifact templates before a new workspace id exists.
Those templates render to broad `ws_*` globs, so ordinary repository files such
as `docs/ws_protocol.md` can be removed only from the request side and overlap
warnings are missed during create and retry.

Scope is limited to owned-path artifact matching plus the repository overlap
regression that exercises create/retry's shared lookup path.

## Requirements Checklist

- Preserve requested real repository paths such as `docs/ws_protocol.md` when a
  custom `docs/{workspace_id}.md` planning template is present and the requested
  workspace id is not known yet.
- Continue filtering actual generated custom planning artifact filenames when
  the workspace id is unknown.
- Continue filtering concrete custom planning artifact filenames when the
  workspace id is known.
- Keep existing default `docs/awf-plans` behavior unchanged.
- Run only focused validation for the changed helper and repository behavior.

## Implementation Steps

1. Add regression coverage showing an unknown-id custom template still filters a
   generated-looking workspace artifact but keeps `docs/ws_protocol.md`.
2. Add repository overlap coverage proving a requested `docs/ws_protocol.md`
   overlap is reported under a custom `docs/{workspace_id}.md` profile.
3. Narrow custom workspace-id glob matching to the generated workspace id shape
   instead of any `ws_*` filename.
4. Update existing custom artifact overlap fixtures to use generated-shaped
   workspace ids where they model generated artifacts.
5. Run focused unit tests and lint for the touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_unknown_custom_plan_template_keeps_real_ws_docs tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`

Full AWF/GitHub validation is managed after agent completion by AWF and CI.
