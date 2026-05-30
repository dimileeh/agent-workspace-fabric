# PRRT_kwDOSJAM6s6F3pSQ Direct Create Workspace Id Plan

## Problem Statement And Scope

Direct workspace creation computes owned-path overlap warnings before the new
workspace row is inserted. For inline or non-auto profiles with custom planning
artifact paths such as `docs/{workspace_id}.md`, overlap filtering therefore has
no concrete requested workspace id and expands the artifact path to
`docs/ws_*.md`. That can classify a real requested repository file shaped like a
workspace artifact as internal and skip the overlap warning.

Scope is limited to direct create id allocation, owned-path overlap lookup, and
focused service-level regression coverage.

## Requirements Checklist

- Direct create must pass the concrete requested workspace id into owned-path
  overlap filtering before coordination warnings are built.
- The created workspace row must use the same id that was used for overlap
  filtering.
- Real `ws_*.md` repository files under a custom planning path must still
  produce coordination warnings when another active workspace owns the same
  file.
- Existing repository create callers must keep their default id generation
  behavior when they do not preallocate an id.

## Implementation Steps

1. Add a service-level regression for direct create with an inline planning
   profile using `docs/{workspace_id}.md` and a real `docs/ws_*.md` requested
   owned path.
2. Confirm the regression fails before the implementation.
3. Allow `WorkspaceRepository.create()` to accept an optional preallocated
   `workspace_id` while preserving the existing generated-id default.
4. Preallocate a direct-create workspace id, pass it to overlap filtering, and
   pass the same id into `repo.create()`.
5. Run focused tests and lint for the touched Python files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_owned_path_policy.py::test_direct_create_custom_plan_path_keeps_real_ws_file_overlap_warning -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_owned_path_policy.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_001.py::TestCreate::test_create_returns_workspace_in_requested_state -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_create.py src/awf/db/repositories/workspace_repo.py tests/unit/service/test_workspace_owned_path_policy.py`

Full AWF/GitHub validation is intentionally left to the AWF post-agent workflow.
