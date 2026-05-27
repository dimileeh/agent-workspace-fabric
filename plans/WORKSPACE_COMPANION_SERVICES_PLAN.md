# Cross-Repo Companion Services Through Workspace Create

## Summary

Wire managed cross-repo companion services through workspace creation. REST,
MCP, and CLI callers will be able to request additional repositories that AWF
checks out beside the primary workspace and renders as companion services in
the outer workspace Compose stack.

## Key Changes

- Add a top-level `companions` list to `WorkspaceCreateRequest`, with
  repo-relative paths and validation for names, dependency targets, ports, and
  path traversal.
- Persist normalized companion requests in `task_policy["companions"]` so
  idempotency, retry, and operator visibility use existing workspace metadata
  instead of a new database column.
- Extend MCP `awf_create_workspace` and CLI `awf workspace create` with
  companion parity. CLI uses repeatable `--companion-json`.
- During provisioning, clone each companion repo with `GitManager.add_worktree`
  using deterministic companion workspace IDs, then pass the materialized
  layouts to the stack launcher.
- Resolve companion build/env/volume paths inside each managed companion
  worktree and feed existing `CompanionService` objects into
  `WorkspaceComposeSpec`.
- Extend cleanup, GC, and orphan-resource detection so companion worktrees are
  retained while the parent workspace exists and removed/classified with the
  parent workspace lifecycle.
- Update OpenAPI/docs/contract metadata and add focused tests for schema,
  REST/MCP/CLI parity, provisioning, compose rendering, and cleanup/orphans.

## Test Plan

- Unit tests for companion request validation and persistence.
- Contract tests for REST/MCP/CLI surface parity.
- Provisioner and stack-launcher tests for materialization and path resolution.
- Cleanup, GC, and orphan-resource tests for companion worktree ownership.
- Focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api tests/unit/mcp tests/unit/cli tests/unit/contracts tests/unit/node tests/unit/service -q -n 20
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

## Assumptions

- Companion paths are repo-relative and resolved only inside the managed
  companion checkout.
- Companions share the parent workspace resource reservation in this slice.
- Raw host-path companions, secret env-file copying, companion PR authorship,
  and per-companion resource accounting remain out of scope.
