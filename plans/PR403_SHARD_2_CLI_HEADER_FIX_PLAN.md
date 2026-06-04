# PR403 Shard 2 CLI Header Fix Plan

## Problem

GitHub coverage shard 2 failed after commit `5eea2c8d` because
`tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_minimal_development_payload`
still expected `workspace_create(..., api_token=None)` to send no auth headers.

That expectation is now stale: PR #403's review feedback required general host
CLI calls to discover the root Compose local default token, so a fresh
`docker compose up --build` stack can be controlled without manually exporting
`AWF_API_TOKEN`.

## Plan

- Update the workspace-create helper test to expect the local Compose default
  `Authorization` header when no explicit token is supplied.
- Keep explicit token precedence tests unchanged.
- Validate the focused failing test, the workspace command helper file, and
  local pytest-split group 2.
- Commit and push the narrow test fix.
