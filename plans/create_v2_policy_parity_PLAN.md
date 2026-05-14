# Plan: P1 create-v2 policy parity

## Objective
Implement remaining REST v2 workspace policy parity for `awf_create_workspace_v2` and `awf workspace create` so both expose `out_of_scope_changes` and `provider_recovery` in task policy.

## Scope
- MCP: add optional tool inputs and pass-through into `WorkspaceCreateV2Request.task`.
- CLI: add explicit JSON flags for both policy objects, parse JSON locally, and include fields under request `task`.
- Contract tests: update canonical alignment + surface metadata assertions and capability registry rows.
- MCP tests: verify payload includes the new policy fields and MCP tooling shape remains stable.
- CLI tests: verify encoded task payload and malformed JSON short-circuits before request.
- Docs and readiness tracker: mark slice implemented in `docs/MCP_CLIENT_PARITY.md` and `TODO/pre-gke-industrial-readiness.md`.

## Work plan
1. Update `src/awf/mcp/server.py`:
   - Extend `awf_create_workspace_v2` signature with optional `out_of_scope_changes` and `provider_recovery` inputs (dict typed).
   - Include both keys in the nested `task` dict in `WorkspaceCreateV2Request` construction.

2. Update CLI `workspace_create` in `src/awf/cli/main.py`:
   - Add `--out-of-scope-changes-json` and `--provider-recovery-json` options (string values).
   - Parse JSON locally with clear CLI exit on `json.JSONDecodeError` before request.
   - Inject parsed values into `body["task"]` only when provided.

3. Update contract/test coverage:
   - `tests/unit/contracts/_capabilities.py`: add both fields to `create_workspace_v2.mcp_request_fields` and `create_workspace_v2.cli_options`; set parity status implemented.
   - `tests/unit/contracts/test_request_payload_alignment.py`: include both fields in REST payload and MCP args canonical test.
   - `tests/unit/contracts/test_surface_metadata_alignment.py`: status test will assert implemented when MCP tool exposes both fields.

4. Add targeted unit tests:
   - `tests/unit/mcp/test_mcp_server.py`: assert MCP v2 schema includes both new properties and create-v2 workspace persists task policy fields.
   - `tests/unit/cli/test_cli.py`: verify task body includes both policy objects and malformed JSON does not hit `httpx.request`.

5. Update documentation and backlog tracker:
   - `docs/MCP_CLIENT_PARITY.md`: mark Workspace create v2 status as `MCP implemented` and backlog slice `—`.
   - `TODO/pre-gke-industrial-readiness.md`: mark `TODO§create-v2-parity` complete with workspace/PR evidence placeholder.

## Completion criteria
- Both MCP and CLI command surfaces accept and forward the two v2 policy fields.
- Contract tests for request payload and parity metadata pass with v2 marked implemented.
- Local CLI JSON parse failures return exit code 2 and skip HTTP call.
- Docs and readiness tracker reflect completion.
- Edge cases are explicitly included in done criteria and test evidence:
  - JSON policy flag malformed input is rejected before any request and exits with code 2.
  - Policy field inclusion is optional per flag: one, both, or neither policy JSON flag may be set.
  - Requests without either policy flag still pass through normal task payload behavior.
