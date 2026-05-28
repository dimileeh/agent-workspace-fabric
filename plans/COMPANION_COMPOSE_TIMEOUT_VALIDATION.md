# Companion Compose-Up Timeout Validation

## Result

Implemented the issue #291 timeout fix against `codex/companion-env-secrets` for
PR #292. Companion requests now accept `compose_up_timeout_seconds`, the value
persists through task policy, runtime stack launch computes the effective
timeout from profile and companions, and Compose uses that timeout for
`--wait-timeout` with a 60 second subprocess capture buffer.

## Evidence

- Targeted regression tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py tests/unit/node/test_companion_services.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/cli/test_cli_parts/test_cli_part_001.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py -q`
  - Passed: `365 passed in 44.25s`
- Formatting:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/api/schemas_companions.py src/awf/node/companion_services.py src/awf/node/compose_manager.py src/awf/node/stack_launcher.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py tests/unit/node/test_companion_services.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/cli/test_cli_parts/test_cli_part_001.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py`
  - Passed: `13 files already formatted`
- Ruff:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py src/awf/node/companion_services.py src/awf/node/compose_manager.py src/awf/node/stack_launcher.py tests/unit/api/test_schema_coverage_edges.py tests/unit/api/test_openapi_artifact.py tests/unit/node/test_companion_services.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/cli/test_cli_parts/test_cli_part_001.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py tests/unit/node/test_compose_manager_subprocess.py`
  - Passed: `All checks passed!`
- Mypy:
  `uv run --python 3.12 --extra dev mypy src/awf/api/schemas_companions.py src/awf/node/companion_services.py src/awf/node/compose_manager.py src/awf/node/stack_launcher.py`
  - Passed: `Success: no issues found in 4 source files`
- OpenAPI:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed: `OK: openapi.json matches the current app spec.`

## Notes

- REST/OpenAPI, MCP, and CLI companion payload paths are covered.
- No top-level workspace-create timeout field or dedicated CLI flag was added.
- Full coverage remains delegated to GitHub/AWF CI.
