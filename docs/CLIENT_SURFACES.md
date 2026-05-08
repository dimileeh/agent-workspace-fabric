# AWF Client Surfaces

The AWF REST API is the canonical control-plane interface.

When the service is running locally, interactive API documentation (Swagger/OpenAPI UI) is available at [http://localhost:8000/docs](http://localhost:8000/docs).

## Supported Client Surfaces (v0.1)

REST, CLI, and MCP are the supported client surfaces for v0.1.

AWF does not ship with a supported Python SDK or client abstraction for v0.1. Integrators should use the REST API or the CLI directly.

**Warning:** Do not import internal AWF modules to build custom API clients. Internal paths (e.g., inside `awf.*`) are not part of the stable public contract and will break without notice.

Existing GitHub PR monitor adoption is supported through all three client
surfaces: `POST /v1/workspaces/adopt-pr`, `awf workspace adopt-pr`, and
`awf_adopt_pull_request_monitor`. See
[PR Monitor Adoption](PR_MONITOR_ADOPTION.md) for the operator runbook and
mocked-local demo path.
