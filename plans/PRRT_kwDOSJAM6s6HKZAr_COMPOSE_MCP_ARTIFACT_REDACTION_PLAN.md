# PRRT_kwDOSJAM6s6HKZAr Compose MCP Artifact Redaction Plan

## Problem Statement And Scope

The unresolved PR review thread reports that MCP artifact and payload redaction can miss provider credentials that are present only in a resolved Docker Compose env file. The current MCP artifact path redacts settings values, provider readiness environment values, and recognizable token patterns, but the workspace artifact tool does not receive the `compose_env_file` already passed to `build_mcp_server`.

Scope is limited to the MCP redaction path used by workspace artifact reads and safe MCP payloads. This plan does not change GitHub interaction, merge policy, broad validation, or unrelated secret-detection behavior.

## Requirements Checklist

- Add a failing regression for an `awf_read_workspace_artifact` text artifact containing a provider secret from a Compose env file when that secret is absent from `os.environ`.
- Thread the resolved Compose/provider secret values into MCP artifact content redaction before base64 encoding.
- Apply the same exact-secret source to MCP safe payload redaction so non-artifact payload strings use the same safety boundary.
- Keep binary artifact behavior conservative by blocking binary content containing those exact Compose/provider secret values.
- Run focused unit tests for the changed MCP behavior only; leave full AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a focused unit test in the existing MCP artifact test module using a temporary Compose env file and an opaque `ANTHROPIC_AUTH_TOKEN` value.
2. Confirm the new test fails against the current implementation.
3. Add a small helper in `src/awf/mcp/server.py` to derive exact secret values from `resolve_local_service_provider_environ` for known provider secret keys.
4. Pass the `compose_env_file` from `build_mcp_server` into workspace tool registration and use the derived values for text redaction and binary secret detection.
5. Update focused tests and run a narrow type or lint check only if the patch shape warrants it.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q`
  - Passes after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase because AWF owns broad validation and provenance after completion.
