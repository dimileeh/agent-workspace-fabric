# Issue 310 Cursor Agent Runtime Validation

## Plan Alignment

- Add `AgentRuntime.cursor` and a new `CursorAdapter`: Complete.
  The adapter invokes `cursor-agent -p --force`, keeps the AWF prompt on stdin,
  emits `--output-format text`, and passes `-m <model>` when a model is selected.
- Document Cursor CLI contract and effort mapping: Complete.
  The adapter docstring cites the official Cursor CLI overview and headless docs.
  Cursor has no documented portable effort flag, so explicit models are preserved
  and high/xhigh/max without a model map to the documented thinking-capable
  `sonnet-4-thinking` model variant.
- Register defaults and adapter registry wiring: Complete.
  Cursor defaults are `model="sonnet-4-thinking"` and `effort="xhigh"`.
- Wire env-only auth/readiness: Complete.
  `CURSOR_API_KEY` is propagated as a placeholder into workspace agent
  environments, included in provider readiness secret handling, and no
  `~/.cursor` auth mount was added.
- Add provider failure/recovery classification: Complete.
  Cursor failures classify to provider `cursor`; recovery keeps Cursor as the
  provider even when the selected model name resembles Anthropic or OpenAI.
- Add Docker runtime install wiring: Complete.
  The agent-runtime image uses `curl https://cursor.com/install -fsS | bash`,
  verifies `cursor-agent`, and documents that Cursor currently tracks the
  official installer because no stable Linux installer version pin is documented.
- Wire smoke/doctor/usage/help/docs/OpenAPI surfaces: Complete.
  Operator-facing provider lists include Cursor. Usage remains reason-coded
  unsupported because the pinned `ccusage@20.0.3` does not expose a Cursor source.
- Preserve existing runtimes: Complete.
  Existing Codex, Claude Code, Gemini, and OpenCode adapters remain registered
  and covered by the shared adapter tests.

## Validation Evidence

- TDD red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k CursorAdapter`
  failed as expected with `ModuleNotFoundError: No module named 'awf.adapters.cursor'`.
- Adapter/defaults/failure classification:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py tests/unit/adapters/test_provider_failures.py -q`
  passed: `59 passed`.
- Readiness/auth/Dockerfile/doctor/usage/status/health/workspace identity:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_003.py tests/unit/service/test_doctor.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/test_agent_runtime_dockerfile.py tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q`
  passed: `400 passed`.
- Cursor selected-preflight focused re-run:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "cursor or selected_provider_preflight_maps_agents"`
  passed: `4 passed, 44 deselected`.
- Dockerfile install-path regression re-run:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passed: `7 passed`.
- CLI/MCP focused provider/runtime coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_service_commands_edges.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_001.py -q -k "cursor or agent or provider or readiness"`
  passed: `15 passed, 101 deselected`.
- MCP provider cleanup re-run:
  `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_001.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_002.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k "provider or readiness or cursor"`
  passed: `8 passed, 100 deselected`.
- OpenAPI artifact/schema checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_schema_coverage_edges.py -q -k "AgentRuntime or agent or workspace_create_schema or openapi"`
  passed: `23 passed, 75 deselected`.
- Operator docs/reason catalog checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_troubleshooting_guide.py tests/unit/docs/test_catalog_coverage.py tests/unit/docs/test_api_surface_cleanup_docs.py tests/unit/docs/test_public_docs_status.py -q`
  passed: `30 passed`.
- Env example checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_env_example.py -q`
  passed: `2 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/__init__.py src/awf/db/enums.py src/awf/adapters src/awf/service/provider_readiness.py src/awf/service/provider_recovery.py src/awf/profiles/compose.py src/awf/service/doctor src/awf/service/smoke.py src/awf/service/usage_collection.py src/awf/service/usage_store.py src/awf/cli/service_commands.py src/awf/cli/workspace_commands.py src/awf/mcp/metrics_tools.py tests/unit/adapters/test_adapters.py tests/unit/adapters/test_provider_failures.py tests/unit/service/test_provider_readiness_parts tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_003.py tests/unit/test_agent_runtime_dockerfile.py tests/unit/service/test_doctor.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/docs/test_catalog_coverage.py tests/unit/mcp/test_mcp_server_parts tests/unit/api/test_workspaces_parts`
  passed: `All checks passed!`
- Focused type check:
  `uv run --python 3.12 --extra dev mypy src/awf/__init__.py src/awf/db/enums.py src/awf/adapters src/awf/service/provider_readiness.py src/awf/service/provider_recovery.py src/awf/profiles/compose.py src/awf/service/doctor src/awf/service/smoke.py src/awf/service/usage_collection.py src/awf/service/usage_store.py src/awf/cli/service_commands.py src/awf/cli/workspace_commands.py src/awf/mcp/metrics_tools.py`
  passed: `Success: no issues found in 25 source files`.
- OpenAPI drift:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  initially reported expected drift from the new `AgentRuntime.cursor` enum.
  After `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`,
  the same `--check` command passed: `OK: openapi.json matches the current app spec.`

## Deferred Validation

- Real end-to-end validation with a rebuilt `awf-agent-runtime` image, an installed
  `cursor-agent` binary, and a real `CURSOR_API_KEY` is deferred to a human/AWF
  environment. This sandbox does not have the real Cursor CLI or key, and tests
  intentionally assert constructed argv/env rather than shelling out to Cursor.
- Broad AWF/GitHub-owned gates, full coverage, full repository pytest, and CI
  validation are deferred to AWF after agent completion per the workspace contract.
- PR description should include: `Closes #310`.
