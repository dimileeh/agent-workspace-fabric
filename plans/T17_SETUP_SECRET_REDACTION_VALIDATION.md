# T17 Setup Secret Redaction Validation

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

## Requirement Status

- Complete: Support bundles include setup config/provider/client/consent/source
  state without raw credential references or plain-file paths.
- Complete: Token-shaped strings are redacted in setup/start logs and
  diagnostics.
- Complete: Provider refs such as `keyring://`, `env://`, and `plain-file://`
  are redacted in generic text surfaces.
- Complete: Plain-file secret paths are omitted or redacted while preserving
  backend/ref kind and credential-ref presence diagnostics.
- Complete: MCP structured payloads, binary/text artifact screening, and
  workspace log reads cannot expose raw setup secrets or provider refs.
- Complete: Existing first-run rendering behavior was left unchanged.

## Evidence

Files changed:

- `src/awf/common/redaction.py`
- `src/awf/service/support_bundle.py`
- `src/awf/service/doctor/__init__.py`
- `src/awf/service/logs.py`
- `src/awf/mcp/server.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/service/test_support_bundle.py`
- `tests/unit/runtime/test_log_redaction.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `tests/unit/service/test_doctor.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_doctor.py -q
# 87 passed

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py -q
# 38 passed

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/service/test_support_bundle.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_doctor.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 6 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Gaps

None found.
