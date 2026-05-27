# Cross-Repo Companion Services Validation

## Summary

Implemented managed cross-repo companion services for workspace creation across
REST, MCP, CLI, provisioning, stack launch, cleanup, orphan detection, docs,
OpenAPI, and contract metadata.

Companion requests are normalized through the canonical
`WorkspaceCreateRequest`, persisted under `task_policy["companions"]`, cloned
as deterministic managed worktrees, converted into compose companion services,
and removed or retained with the parent workspace lifecycle.

## Implementation Notes

- Added `WorkspaceCompanionRequest` with validation for service names,
  reserved names, duplicate names, path containment, volume source safety,
  port syntax, and default base branch behavior.
- Added companion payload support to REST workspace create, MCP
  `awf_create_workspace`, and CLI repeatable `--companion-json`.
- Included companions in idempotency comparison and task policy snapshots.
- Added managed companion worktree helpers using
  `<workspace_id>__companion__<name>` and
  `<branch_prefix>/<workspace_id>/companion/<name>`.
- Materialized companion worktrees before stack launch through the existing
  `GitManager` and passed normalized/materialized companion layouts into the
  stack launcher.
- Resolved companion build contexts, dockerfiles, env files, and relative
  volume sources inside managed companion worktrees before rendering compose.
- Added companion/profile service collision checks and dependency target
  validation before compose render.
- Extended cleanup, destroy, GC, and orphan-resource detection to recognize
  companion worktrees as children of the parent workspace.
- Updated docs, OpenAPI, and contract capability metadata.

## Validation

### Focused Companion Suite

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_schema_coverage_edges.py \
  tests/unit/api/test_workspaces_direct.py \
  tests/unit/contracts/test_request_payload_alignment.py \
  tests/unit/cli/test_workspace_commands_helpers.py \
  tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_001.py \
  tests/unit/node/test_stack_launcher.py \
  tests/unit/node/test_cleanup.py \
  tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py \
  tests/unit/service/test_orphan_resources.py \
  tests/unit/service/test_orphans.py \
  tests/unit/service/test_gc_more2.py \
  -q
```

Result: `267 passed in 65.01s`.

### OpenAPI Artifact Tests

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q
```

Result: `19 passed in 1.50s`.

### Broad Touched Surface

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/api tests/unit/mcp tests/unit/cli tests/unit/contracts \
  tests/unit/node tests/unit/service \
  -q -n 20
```

Result: `4253 passed in 511.79s`.

### Maintainability Guard

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
```

Result: `9 passed in 2.62s`.

### Static Checks

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_metrics_parts/test_metrics_part_001.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/api/test_openapi_artifact.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_workspaces_direct.py tests/unit/contracts/test_request_payload_alignment.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_001.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_cleanup.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py tests/unit/service/test_orphan_resources.py tests/unit/service/test_orphans.py tests/unit/service/test_gc_more2.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 276 source files`.

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

Result: `OK: openapi.json matches the current app spec.`

### Full Coverage

```bash
env -u CI \
  AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@127.0.0.1:5433/awf_cov \
  AWF_TEST_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@127.0.0.1:5433/awf_cov \
  uv run --python 3.12 pytest \
    -n 20 --dist=loadscope --timeout=300 \
    --cov=awf --cov-report=term-missing --cov-report=xml \
    --cov-report=json:/tmp/awf-coverage-current.json \
    --cov-fail-under=99
```

Result: `8058 passed, 1 skipped in 588.99s`; coverage gate passed with
`Total coverage: 99.00%`.

The local run unsets `CI` because this Mac does not have passwordless sudo, and
`tests/integration/test_workspace_agent_git_in_workspace.py` intentionally hard
fails under `CI=true` unless the process can chown the prepared worktree as
root, UID 1000, or via passwordless sudo. GitHub Actions' `ubuntu-latest`
runner has passwordless sudo, so the CI command exercises that test instead of
skipping it.

## Gaps

- The exact GitHub `CI=true` environment cannot be reproduced on this local Mac
  because passwordless sudo is unavailable. The same coverage command shape was
  run locally with `CI` unset and the isolated `awf_cov` database.
