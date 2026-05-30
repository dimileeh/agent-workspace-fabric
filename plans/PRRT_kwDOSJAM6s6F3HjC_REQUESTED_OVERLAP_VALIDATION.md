# PRRT_kwDOSJAM6s6F3HjC Requested Overlap Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F3HjC_REQUESTED_OVERLAP_PLAN.md`

## Requirement Status

- Preserve requested real repository paths such as `docs/ws_protocol.md` when a
  custom `docs/{workspace_id}.md` planning template is present and the requested
  workspace id is not known yet: Complete.
- Continue filtering actual generated custom planning artifact filenames when
  the workspace id is unknown: Complete.
- Continue filtering concrete custom planning artifact filenames when the
  workspace id is known: Complete.
- Keep existing default `docs/awf-plans` behavior unchanged: Complete.
- Run only focused validation for the changed helper and repository behavior:
  Complete.

## Evidence

Files changed:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `plans/PRRT_kwDOSJAM6s6F3HjC_REQUESTED_OVERLAP_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F3HjC_REQUESTED_OVERLAP_VALIDATION.md`

Failing-before evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_unknown_custom_plan_template_keeps_real_ws_docs tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap -q`
  - Result before implementation: 2 failed; `docs/ws_protocol.md` was filtered
    from requested paths and the repository overlap lookup returned `[]`.

Passing focused checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::test_unknown_custom_plan_template_keeps_real_ws_docs tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap -q`
  - Result: 2 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_profile_unknown_requested_workspace_keeps_real_ws_docs_overlap -q`
  - Result: 30 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup -q`
  - Result: 46 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/common/test_owned_paths.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/common/owned_paths.py`
  - Result: passed.

Full AWF/GitHub validation is managed after agent completion by AWF and CI; it
was not run locally.

## Gaps

None.
