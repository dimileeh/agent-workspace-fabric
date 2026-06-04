# T17 Setup Secret Redaction Plan

Plan source: `docs/awf-plans/ws_66f87dca61764f249e95853d.md`

## Problem Statement And Scope

T06/T07 added setup credential references and provider setup orchestration. T17
hardens setup/start operator diagnostics so support bundles, logs, doctor output,
and MCP surfaces expose useful setup state without leaking raw provider tokens,
provider references, or sensitive plain-file secret paths.

Scope is limited to redaction and sanitized setup diagnostics. No branch
switching, pushing, broad validation, coverage gates, setup redesign, new MCP
credential entry, or unrelated refactors are included.

## Assumptions/Changes

- Review thread `PRRT_kwDOSJAM6s6G_-Om` identified a remaining MCP log-read
  gap: redacting only the already requested byte slice can leak configured
  secret substrings when callers request overlapping offsets through a raw log
  file.
- This repair remains inside the existing T17 redaction scope and only changes
  MCP log-read redaction plus focused regressions.
- Review thread `PRRT_kwDOSJAM6s6HABmr` identified that unexpected
  setup-config reader exceptions still abort support-bundle collection instead
  of returning a redacted failed setup-state payload.
- This repair remains inside the existing T17 support-bundle setup-state scope
  and only changes unexpected reader exception handling plus a focused
  regression.

## Requirements Checklist

- Support bundles include setup config/provider/client/consent/source-checkout
  state without raw credential references or plain-file paths.
- Token-shaped strings are redacted in setup/start logs and diagnostics.
- Provider refs such as `keyring://`, `env://`, and `plain-file://` are redacted
  in generic text surfaces.
- Plain-file secret paths are omitted or redacted while preserving backend/ref
  kind and credential-ref presence diagnostics.
- MCP structured/text/artifact/log surfaces cannot expose raw setup secrets or
  provider refs.
- MCP workspace log reads redact with enough surrounding context that arbitrary
  requested offsets cannot reveal substrings of configured secrets.
- Existing first-run rendering behavior remains compatible.

## Implementation Steps

1. Add focused failing tests for common redaction, support bundles, service log
   capture, doctor output, MCP payload redaction, and MCP workspace log reads.
2. Extend shared redaction to cover setup provider refs and plain-file paths.
3. Route doctor, service logs, MCP payload/log reads, and support-bundle text
   boundaries through the shared redaction helper.
4. Add a sanitized support-bundle setup-state collector with an injectable host
   setup config reader for tests.
5. Keep provider summaries to status/source/backend/ref presence/ref kind, client
   summaries to status/update time, consent to booleans, and source-checkout to
   configured/verified metadata without local root paths.
6. Create `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` after implementation
   with requirement-by-requirement evidence.
7. Add a focused regression for a raw MCP log whose requested slice starts
   inside a configured secret, confirm it fails, then redact an expanded log
   window before returning the requested slice.
8. Add a focused regression for an unexpected setup-config reader exception,
   confirm it fails, then return a redacted failed setup-state payload while the
   rest of support-bundle collection succeeds.

## Verification Commands

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_doctor.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py -q
```

Review-thread repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
```

Review-thread `PRRT_kwDOSJAM6s6HABmr` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k 'setup_state_degrades_unexpected_config_reader_errors or setup_state_redacts_config_load_errors'
uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py tests/unit/service/test_support_bundle.py
uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py
```

Focused lint/type checks, adjusted to touched files:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/service/test_support_bundle.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_doctor.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
are intentionally left to AWF after agent completion.
