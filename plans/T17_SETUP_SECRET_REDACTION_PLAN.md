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
- Review thread `PRRT_kwDOSJAM6s6HAjVz` identified that MCP log reads can still
  expose pattern-only secret assignment values when the requested offset starts
  more than the fixed redaction context into a long `TOKEN=`/`PASSWORD=` value.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes MCP log-read handling plus a focused regression.
- Review-level comment `issue:4620175517` identified two final hardening points:
  document the retained MCP binary secret-detection regex safety nets, and make
  loaded setup-state summarization degrade if malformed config data raises after
  the reader succeeds.
- This repair remains inside the existing T17 redaction/support-bundle scope and
  only changes the MCP comment, setup-state loaded-path guard, focused
  regression, and this plan/validation evidence.
- Review thread `PRRT_kwDOSJAM6s6HBBcY` identified that MCP workspace log reads
  can expand the read offset into the middle of a multibyte UTF-8 sequence,
  causing decoded replacement bytes to shift later byte-window projection.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes byte-preserving MCP log-read projection plus a focused regression.
- Review-level comment `issue:4620175517` identified that MCP workspace log
  reads assume a non-EOF expanded read always reaches the full requested caller
  window. If a future log backend returns a short non-EOF expanded read, the MCP
  response must not advance `next_offset` past bytes actually covered by that
  expanded result.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes requested-window offset projection plus a focused regression.

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
- MCP workspace log reads preserve requested byte offsets when redaction context
  expansion starts inside a multibyte UTF-8 character.
- MCP workspace log reads do not expose pattern-only secret assignment values
  when the assignment key prefix is outside the fixed context window.
- MCP workspace log reads do not skip data if the expanded log read is short
  without EOF; `next_offset` advances only through the actually covered caller
  window.
- Support-bundle setup-state collection returns a redacted failed setup-state
  payload if loaded config summarization raises after the config reader
  succeeds.
- MCP binary secret detection documents why service-side token/URL regexes are
  retained after the shared redaction guard.
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
9. Add a focused regression for a raw MCP log whose requested slice starts deep
   inside a pattern-only `TOKEN=` value, confirm it fails, then ensure the MCP
   log read redacts an unknown leading token fragment instead of returning it as
   ordinary text.
10. Add a focused regression for loaded setup-state summarization failure,
    confirm it fails, then catch and redact summary-building exceptions without
    discarding the rest of the support bundle.
11. Add a concise comment explaining the retained MCP binary secret-detection
    safety nets after the shared `redact_secrets` guard.
12. Add a focused regression where the expanded MCP log-read context starts
    inside a multibyte character, confirm it fails, then preserve raw log bytes
    through MCP byte-window projection.
13. Add a focused regression for a short non-EOF expanded MCP log read, confirm
    it fails, then project the returned `next_offset` to the actual caller-window
    bytes covered by the expanded result.

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

Review-thread `PRRT_kwDOSJAM6s6HAjVz` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k pattern_only_secret_assignment
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

Review-level comment `issue:4620175517` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_loaded_config_summary_errors
uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py src/awf/mcp/server.py tests/unit/service/test_support_bundle.py
uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py src/awf/mcp/server.py
```

Review-thread `PRRT_kwDOSJAM6s6HBBcY` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k expanded_context_starts_inside_multibyte_character
uv run --python 3.12 --extra dev ruff check src/awf/runtime/logs.py src/awf/service/workspaces.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/logs.py src/awf/service/workspaces.py src/awf/mcp/metrics_tools.py
```

Review-level comment `issue:4620175517` short-read repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k short_non_eof_expanded_read
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

Focused lint/type checks, adjusted to touched files:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/service/test_support_bundle.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_doctor.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/service/support_bundle.py src/awf/service/doctor/__init__.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
are intentionally left to AWF after agent completion.

## Review-Level Comment `issue:4620175517` Collision/Encoding Repair Plan

### Problem Statement and Scope

The review-level comment identifies two narrow edge cases in the T17 redaction
work:

- `setup_state.providers` and `setup_state.clients` key entries by redacted
  provider/client names, so two raw names that both redact to `<redacted>` can
  silently overwrite each other.
- `_unknown_leading_log_value_fragment_end` encodes the entire expanded log
  window before it knows whether the first byte is a delimiter, wasting memory
  on every MCP log-read call whose expanded projection starts after byte zero.

This repair is limited to those two behaviors and their focused regressions.

### Requirements Checklist

- Preserve every configured provider and client in setup-state output even when
  multiple raw names redact to the same display key.
- Keep raw provider/client names and token-like fragments out of support-bundle
  setup-state output.
- Preserve existing setup-state payload shape for non-colliding names.
- Avoid encoding the entire expanded log projection in
  `_unknown_leading_log_value_fragment_end` just to check the first byte.
- Preserve existing unknown-leading-fragment delimiter behavior and MCP log
  read output contracts.

### Implementation Steps

1. Add focused failing tests for setup-state provider/client redacted-name
   collisions and for the leading-fragment helper's first-character fast path.
2. Add a small helper that inserts redacted setup-state entries with a stable
   numeric suffix when a redacted key collision occurs.
3. Refactor `_unknown_leading_log_value_fragment_end` to inspect the first
   character before scanning and then encode characters incrementally until a
   delimiter is found.
4. Run focused tests and lint/type checks for the touched files only.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence from the focused checks. Full AWF/GitHub validation
   remains owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_preserves_redacted_name_collisions
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k unknown_leading_log_value_fragment_end
uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py src/awf/mcp/metrics_tools.py tests/unit/service/test_support_bundle.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py src/awf/mcp/metrics_tools.py
```
