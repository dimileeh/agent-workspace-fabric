# PRRT_kwDOSJAM6s6DaP8C Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6DaP8C` flags the preserved-active-execution replacement path in `src/awf/control/worker.py` for manually copying many `Workspace` fields into `WorkspaceRepository.create`. That copy list is brittle when the `Workspace` creation contract changes.

## Scope

- Keep the fix limited to workspace replacement creation.
- Do not change branch, push, or PR-thread state manually.
- Preserve active-execution salvage behavior and event ordering.

## Requirements

- Add a repository-owned helper for creating a requested replacement workspace from an existing workspace.
- Ensure copied workspace request/profile fields are set during creation, not by post-insert mutation after a state transition.
- Use the helper from preserved active replacement creation in `ControlWorker`.
- Add focused regression coverage for the helper and the worker salvage path.
- Commit the change locally with a conventional commit message tied to the thread id.

## Implementation Steps

1. Add a failing repository test for replacement creation from a source workspace, including current request/profile fields and copy isolation for mutable JSON/list values.
2. Add or update worker salvage test coverage so preserved-active replacement uses the repository helper and preserves request fields.
3. Implement `WorkspaceRepository.create_replacement_from`.
4. Refactor `_create_preserved_active_replacement` to call the helper.
5. Run the narrow unit tests, then run formatting/lint/type checks if the surface warrants it.
6. Write validation results to `plans/PRRT_kwDOSJAM6s6DaP8C_VALIDATION.md`.
