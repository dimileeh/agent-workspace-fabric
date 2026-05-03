# AWF REST API Reference

The AWF REST API is the canonical control-plane interface.

## Supported Client Surfaces (v0.1)

REST, CLI, and MCP are the supported client surfaces for v0.1.

AWF does not ship with a supported Python SDK or client abstraction for v0.1. Integrators should use the REST API or the CLI directly.

**Warning:** Do not import internal Python modules from the AWF codebase to build custom clients. Internal paths (e.g., inside `awf.*`) are not part of the stable public contract and will break without notice.
