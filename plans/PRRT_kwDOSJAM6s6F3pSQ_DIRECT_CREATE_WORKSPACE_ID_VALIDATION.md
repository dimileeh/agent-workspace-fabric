# PRRT_kwDOSJAM6s6F3pSQ Direct Create Workspace Id Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F3pSQ_DIRECT_CREATE_WORKSPACE_ID_PLAN.md`

## Requirement Status

- Complete: Direct create now passes a concrete requested workspace id into
  owned-path overlap filtering before coordination warnings are built.
- Complete: `WorkspaceRepository.create()` accepts the same preallocated
  workspace id and persists it on the created row.
- Complete: Added a regression proving a real `docs/ws_*.md` requested owned
  path under an inline custom `docs/{workspace_id}.md` planning profile records
  an overlap coordination warning.
- Complete: Existing repository create callers keep default id generation when
  no preallocated id is provided.

## Evidence

Files changed:

- `src/awf/service/workspaces_create.py`
- `src/awf/db/repositories/workspace_repo.py`
- `tests/unit/service/test_workspace_owned_path_policy.py`
- `plans/PRRT_kwDOSJAM6s6F3pSQ_DIRECT_CREATE_WORKSPACE_ID_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F3pSQ_DIRECT_CREATE_WORKSPACE_ID_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_owned_path_policy.py::test_direct_create_custom_plan_path_keeps_real_ws_file_overlap_warning -q`
  - Initial red run before implementation: failed with `KeyError: 'coordination'`.
  - Final result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_owned_path_policy.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_001.py::TestCreate::test_create_returns_workspace_in_requested_state -q`
  - Result: 16 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_create.py src/awf/db/repositories/workspace_repo.py tests/unit/service/test_workspace_owned_path_policy.py`
  - Result: passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run locally.

## Gaps

None.
