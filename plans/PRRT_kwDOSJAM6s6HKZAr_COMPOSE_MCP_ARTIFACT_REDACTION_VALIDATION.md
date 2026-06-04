# PRRT_kwDOSJAM6s6HKZAr Compose MCP Artifact Redaction Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6HKZAr_COMPOSE_MCP_ARTIFACT_REDACTION_PLAN.md`

## Requirement Status

- Add a failing regression for an `awf_read_workspace_artifact` text artifact containing a provider secret from a Compose env file when that secret is absent from `os.environ`: Complete.
- Thread the resolved Compose/provider secret values into MCP artifact content redaction before base64 encoding: Complete.
- Apply the same exact-secret source to MCP safe payload redaction so non-artifact payload strings use the same safety boundary: Complete.
- Keep binary artifact behavior conservative by blocking binary content containing those exact Compose/provider secret values: Complete.
- Run focused unit tests for the changed MCP behavior only; leave full AWF/GitHub validation to AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/mcp/server.py`
- `src/awf/mcp/workspace_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py`
- `plans/PRRT_kwDOSJAM6s6HKZAr_COMPOSE_MCP_ARTIFACT_REDACTION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6HKZAr_COMPOSE_MCP_ARTIFACT_REDACTION_VALIDATION.md`

Focused test-first evidence:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py::TestReadWorkspaceArtifact::test_read_workspace_artifact_redacts_compose_env_file_provider_secret -q` failed because the returned artifact content still contained `opaque-compose-value`.

Focused verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py::TestReadWorkspaceArtifact::test_read_workspace_artifact_redacts_compose_env_file_provider_secret tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py::TestReadWorkspaceArtifact::test_binary_artifact_containing_compose_env_file_provider_secret_is_blocked -q` passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q` passed: 32 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py src/awf/mcp/workspace_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py src/awf/mcp/workspace_tools.py` passed.
- `git diff --check` passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation, provenance, logs, timeouts, and merge gating after this repair completes.

## Gaps

No gaps remain against the saved plan.
