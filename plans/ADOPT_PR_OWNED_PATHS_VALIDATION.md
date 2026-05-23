# Adopt-PR Owned Paths Validation

Plan reference: `plans/ADOPT_PR_OWNED_PATHS_PLAN.md`

## Requirement Status

- Complete: REST schema accepts `owned_paths` and `openapi.json` exposes it.
  Evidence: `src/awf/api/schemas.py`, `openapi.json`,
  `tests/unit/api/test_pr_monitor_adoption.py`.
- Complete: Adoption service persists `owned_paths` into the created workspace.
  Evidence: `src/awf/service/pr_monitor_adoption.py`,
  `tests/unit/service/test_pr_monitor_adoption.py`.
- Complete: Task and attempt rows inherit adoption owned paths.
  Evidence: `tests/unit/service/test_pr_monitor_adoption.py`.
- Complete: Live adoption reattach with different owned paths returns a policy
  conflict instead of silently attaching.
  Evidence: `src/awf/service/pr_monitor_adoption.py`,
  `tests/unit/service/test_pr_monitor_adoption.py`.
- Complete: CLI exposes and forwards repeatable `--owned-path`.
  Evidence: `src/awf/cli/main.py`, `tests/unit/cli/test_cli.py`.
- Complete: MCP exposes and forwards `owned_paths`.
  Evidence: `src/awf/mcp/server.py`, `tests/unit/mcp/test_mcp_server.py`.
- Complete: Direct workspace creation keeps supporting protected owned paths
  for assigned tasks.
  Evidence: `tests/unit/cli/test_cli.py`,
  `tests/unit/service/test_workspaces_observability.py`,
  existing protected guard coverage in `tests/unit/control/test_quality_gates.py`.
- Complete: Local service image rebuild has the packaging forced-includes
  available before `uv sync`.
  Evidence: `docker/control-plane.Dockerfile`,
  `tests/unit/cli/test_packaging.py`.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_pr_monitor_adoption.py::test_adoption_request_schema_accepts_model_effort_owned_paths_and_openapi_exposes_fields \
  tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_creates_lineage_and_monitor_owned_request \
  tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_attaching_live_adoption_rejects_different_owned_paths \
  tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_posts_owned_paths_when_requested \
  tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_forwards_model_and_effort -q
# 5 passed

uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/service/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr \
  tests/unit/mcp/test_mcp_server.py -q
# 239 passed

uv run --python 3.12 --extra dev ruff check \
  src/awf/api/schemas.py src/awf/cli/main.py src/awf/mcp/server.py \
  src/awf/service/pr_monitor_adoption.py \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/service/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found

uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
# openapi.json matches
```

Additional direct-create and packaging checks were added after confirming the
PR adoption gap:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_cli.py::TestWorkspaceCreate::test_emits_new_v2_flags_to_post \
  tests/unit/service/test_workspaces_observability.py::test_workspace_service_round_trips_policy_metadata \
  tests/unit/cli/test_packaging.py::test_control_plane_dockerfile_copies_forced_bootstrap_assets_before_uv_sync \
  -q
# 3 passed

uv run --python 3.12 --extra dev pytest \
  tests/unit/api/test_pr_monitor_adoption.py::test_adoption_request_schema_accepts_model_effort_owned_paths_and_openapi_exposes_fields \
  tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_creates_lineage_and_monitor_owned_request \
  tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_attaching_live_adoption_rejects_different_owned_paths \
  tests/unit/cli/test_cli.py::TestWorkspaceAdoptPr::test_posts_owned_paths_when_requested \
  tests/unit/mcp/test_mcp_server.py::TestToolRegistration::test_adopt_pull_request_monitor_tool_forwards_model_and_effort \
  tests/unit/cli/test_cli.py::TestWorkspaceCreate::test_emits_new_v2_flags_to_post \
  tests/unit/service/test_workspaces_observability.py::test_workspace_service_round_trips_policy_metadata \
  tests/unit/cli/test_packaging.py::test_control_plane_dockerfile_copies_forced_bootstrap_assets_before_uv_sync \
  tests/unit/control/test_quality_gates.py::test_explicit_ownership_allows_quality_gate_change \
  -q
# 9 passed

uv run --python 3.12 --extra dev ruff check \
  src/awf/api/schemas.py src/awf/cli/main.py src/awf/mcp/server.py \
  src/awf/service/pr_monitor_adoption.py \
  tests/unit/api/test_pr_monitor_adoption.py \
  tests/unit/service/test_pr_monitor_adoption.py \
  tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py \
  tests/unit/cli/test_packaging.py \
  tests/unit/service/test_workspaces_observability.py \
  tests/unit/control/test_quality_gates.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found
```

## Remaining Gaps

None for this slice.
