# Adopt-PR Owned Paths Plan

## Problem Statement

`awf workspace adopt-pr` creates PR-monitor workspaces without any way for the
operator to declare `owned_paths`. That makes adopted PR monitors unable to
receive explicit ownership for protected files such as `.github/workflows/*.yml`
or `pyproject.toml`; the monitor can then fail with a protected-scope push block
even when the operator intentionally approved that scope.

## Scope

- Add `owned_paths` to the PR adoption request contract.
- Persist those paths onto the created workspace, task, and attempt.
- Expose the same option through the CLI and MCP adoption tool.
- Reject attaching to a live adoption when the requested owned-path policy
  differs from the existing adoption.
- Verify the existing direct workspace-create path still accepts protected
  owned paths for assigned tasks.
- Unblock the local AWF Docker rebuild by copying all packaging forced-includes
  before `uv sync` in the control-plane Dockerfile.
- Keep the existing protected-file guard behavior unchanged.

## Requirements Checklist

- [ ] REST schema accepts `owned_paths` for `PullRequestMonitorAdoptionRequest`
      and OpenAPI exposes the field.
- [ ] Adoption service passes `owned_paths` into `WorkspaceRepository.create`.
- [ ] Task and task attempt rows created for adoption inherit the workspace
      owned paths.
- [ ] Re-adopting a live PR monitor with different owned paths returns a policy
      conflict instead of silently attaching.
- [ ] CLI `awf workspace adopt-pr` supports repeatable `--owned-path`.
- [ ] MCP `awf_adopt_pull_request_monitor` exposes and forwards `owned_paths`.
- [ ] Focused unit tests cover schema/API, service, CLI, and MCP behavior.
- [ ] Direct workspace create tests prove protected paths such as
      `.github/workflows/publish.yml` and `pyproject.toml` can be declared.
- [ ] Docker packaging tests prove all forced bootstrap assets are present
      before the service image runs `uv sync`.

## Implementation Steps

1. Add failing tests for schema/OpenAPI, service persistence/conflict, CLI JSON
   body, and MCP tool forwarding.
2. Add the schema field and route-compatible model behavior.
3. Pass owned paths through the adoption service and conflict checker.
4. Add CLI and MCP parameters.
5. Run focused tests and lint/type checks for touched files.
6. Rebuild/restart AWF and relaunch the affected PR monitor with explicit
   protected-file owned paths.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/service/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr \
  tests/unit/mcp/test_mcp_server.py::TestMcpServer \
  -q

uv run --python 3.12 --extra dev ruff check \
  src/awf/api/schemas.py src/awf/cli/main.py src/awf/mcp/server.py \
  src/awf/service/pr_monitor_adoption.py \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/service/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py

uv run --python 3.12 --extra dev mypy src/awf
```
