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
- Review thread `PRRT_kwDOSJAM6s6HBsS0` identified that if the assignment
  lookback read cannot cover the requested window, MCP workspace log reads fall
  back to the narrower projection and may expose the raw tail of a pattern-only
  `TOKEN=` value.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes failed-lookback leading-fragment masking plus a focused regression.
- Review thread `PRRT_kwDOSJAM6s6HCoIm` identified that invalid UTF-8 bytes
  before the requested window can expand to a three-byte replacement character
  during MCP log projection, shifting subsequent byte-slice offsets away from
  the stored raw log byte offsets.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes byte-preserving projection/redaction for raw log bytes plus a focused
  regression.
- Review thread `PRRT_kwDOSJAM6s6HC827` identified that assignment lookback can
  cover the requested byte window while still starting inside an unknown
  leading token fragment, causing MCP log reads to clear the untrusted-fragment
  flag without proving the assignment key or a safe token boundary is visible.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes lookback projection trust checks plus a focused regression.
- Review-level comment `issue:4620175517` identified three final review
  follow-ups: compare MCP assignment early-break bounds in bytes rather than
  character indexes, document that followed service-log redaction is currently
  line-scoped, and centralize support-bundle setup-state fallback reason codes.
- This repair remains inside the existing T17 redaction/support-bundle scope and
  only changes the targeted helper, explanatory service-log comment,
  setup-state constants, focused regressions, and this plan/validation evidence.
- Review thread `PRRT_kwDOSJAM6s6HDTtb` identified that MCP workspace log exact
  secret redaction scans only the MCP process environment, so provider tokens
  present only in the local Compose env file can leak when the returned slice
  contains the bare value without a visible secret assignment prefix.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes Compose-env exact secret discovery plus a focused regression.
- Review thread `PRRT_kwDOSJAM6s6HDiER` identified the same exact-secret class
  in `awf service logs`: captured and followed Docker Compose service-log output
  only applied pattern redaction, so a provider credential value present only in
  the selected Compose env file could leak when emitted as a bare string.
- This repair remains inside the existing T17 service-log redaction scope and
  only changes Compose-env provider-secret discovery, service-log redaction
  threading, focused regressions, and this plan/validation evidence.

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
- MCP workspace log reads mask an unknown leading value fragment when
  assignment lookback cannot read enough context to prove the fragment safe.
- MCP workspace log reads preserve requested byte offsets when invalid UTF-8
  bytes appear in expanded redaction context before the requested window.
- MCP workspace log reads do not clear unknown-leading-fragment masking merely
  because assignment lookback covers the requested byte window; the widened
  projection must either show assignment context or a safe token boundary.
- MCP workspace log assignment-context early-break logic compares byte offsets
  to byte offsets when multibyte text appears before an assignment.
- MCP workspace log exact-secret redaction includes provider credentials loaded
  from the local Compose env file, even when those values are not exported in
  the MCP process environment.
- Captured and followed service-log output redact exact provider credential
  values loaded from the selected Compose env file, even when those values do
  not match token shape patterns and appear without an assignment or bearer
  prefix.
- Followed service-log streaming documents that the current per-line redaction
  boundary depends on single-line secret/provider-ref patterns.
- Support-bundle setup-state generic fallback reason codes are centralized.
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
14. Add a focused regression for failed assignment lookback, confirm it fails,
    then mask the unknown leading fragment rather than returning the narrower
    raw projection.
15. Add a focused regression for invalid UTF-8 bytes before the requested MCP
    log window, confirm it fails, then preserve raw byte offsets through
    projection/redaction while still rendering invalid bytes with replacement in
    returned text.
16. Add a focused regression for a successful assignment lookback that still
    starts mid-token, confirm it fails, then keep unknown-leading-fragment
    masking unless the widened projection proves assignment context or a safe
    boundary.
17. Add a focused regression proving `_workspace_log_assignment_value_covers_byte`
    breaks before later matches using byte offsets when multibyte text precedes
    an assignment.
18. Change the helper to compute the assignment value byte start before the
    early-break comparison.
19. Add a concise comment to followed service-log streaming documenting the
    line-scoped redaction boundary.
20. Replace setup-state generic fallback reason string literals with shared
    constants and cover the no-`reason_code` reader fallback.
21. Add a focused regression for a Compose-only provider token in an MCP log
    slice without a visible assignment prefix, confirm it fails, then include
    local Compose provider env secrets in the MCP exact-secret set.
22. Add focused regressions for captured and followed service logs containing a
    Compose-only provider secret value without a visible assignment prefix,
    confirm they fail, then include selected Compose env provider secrets in the
    service-log redactor.

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

Review-thread `PRRT_kwDOSJAM6s6HDTtb` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_provider_secret
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

Review-thread `PRRT_kwDOSJAM6s6HDiER` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k compose_env_provider_secret
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/service/logs.py
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

Review-thread `PRRT_kwDOSJAM6s6HBsS0` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_failure
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'assignment_lookback_failure or pattern_only_secret_assignment or preserves_long_benign_token_without_assignment_context'
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

Review-level comment `issue:4620175517` byte-break/comment/constants checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_value_covers_byte_breaks_using_byte_offsets
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_unexpected_config_reader_errors_without_reason_code
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py src/awf/service/logs.py src/awf/service/support_bundle.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/service/test_support_bundle.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py src/awf/service/logs.py src/awf/service/support_bundle.py
```

Review-thread `PRRT_kwDOSJAM6s6HCoIm` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k invalid_utf8_before_requested_window
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'invalid_utf8_before_requested_window or expanded_context_starts_inside_multibyte_character or read_workspace_log_redacts_slice_starting_inside_configured_secret'
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k redact_secrets_byte_slice
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
```

Review-thread `PRRT_kwDOSJAM6s6HC827` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_still_mid_fragment
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'assignment_lookback_still_mid_fragment or assignment_lookback_failure or pattern_only_secret_assignment or preserves_long_benign_token_without_assignment_context'
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

## Review Thread `PRRT_kwDOSJAM6s6HBsyZ` Followed Service Logs Repair Plan

### Problem Statement And Scope

The review thread reports that `awf service logs --follow` invokes Docker with
`capture_output=False`, letting Docker write service logs directly to the
operator terminal before `run_service_logs()` can redact `stdout`/`stderr`.

This repair is limited to followed local service logs. Non-follow log capture,
service selection, Docker environment resolution, support bundles, MCP log
reads, and unrelated CLI behavior are out of scope.

### Requirements Checklist

- Followed service logs must not bypass the shared `redact_secrets()` boundary
  before they are written to the operator terminal.
- Followed service logs should preserve streaming behavior for ordinary Docker
  output.
- Followed service log interrupt handling remains successful for Ctrl-C return
  codes and `KeyboardInterrupt`.
- Non-follow service logs continue to return captured, redacted stdout/stderr.

### Implementation Steps

1. Add/update focused tests showing the default follow runner redacts streamed
   stdout/stderr and no longer relies on Docker writing directly to the terminal.
2. Change the default service-log subprocess runner so `capture_output=False`
   uses piped stdout/stderr, streams redacted lines to the process stdout/stderr,
   and returns a completed-process-like result without captured output.
3. Keep `run_service_logs()` interrupt and non-follow behavior intact, adjusting
   only the follow failure detail wording if needed.
4. Run focused tests and lint/type checks for the touched files only.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with evidence. Broad
   AWF/GitHub validation, full coverage gates, and CI-equivalent suites remain
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or default_subprocess_runner'
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py -q -k service_logs_follow
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review Thread `PRRT_kwDOSJAM6s6HCaLj` Followed Service Logs Interrupt Cleanup Plan

### Problem Statement And Scope

The review thread reports that the custom followed service-log `Popen` path can
receive `KeyboardInterrupt` while waiting for `docker compose logs --follow`,
then return through `run_service_logs()` without terminating or reaping the
Docker child process.

This repair is limited to interrupt cleanup in the default streaming
service-log subprocess runner. Service selection, Docker environment
resolution, redaction behavior, non-follow log capture, and unrelated CLI
behavior are out of scope.

### Requirements Checklist

- A followed service-log interrupt terminates the Docker child before returning
  control to `run_service_logs()`.
- If graceful termination does not finish promptly, the child is killed and
  reaped.
- Reader threads for the child's stdout/stderr are joined during interrupt
  cleanup.
- Existing `run_service_logs(follow=True)` interrupt semantics remain intact:
  the caller receives an empty successful result.

### Implementation Steps

1. Add a focused regression that simulates `KeyboardInterrupt` at
   `process.wait()` and asserts the child is terminated, killed on timeout, and
   reaped before `run_service_logs()` returns.
2. Update the default streaming subprocess runner to handle
   `KeyboardInterrupt` by terminating, escalating to kill after a short timeout,
   waiting for the child, joining reader threads, and re-raising.
3. Run focused service-log tests and lint/type checks for the touched files
   only.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with evidence. Broad
   AWF/GitHub validation, full coverage gates, and CI-equivalent suites remain
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or default_subprocess_runner'
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review-Level Comment `issue:4620175517` Log Redaction Performance Repair Plan

### Problem Statement And Scope

The review-level comment identifies avoidable work in MCP workspace log
redaction:

- `redact_secrets_byte_slice()` builds a full text-index to byte-offset list
  for every secret-bearing byte-slice redaction.
- assignment lookback can issue a second log read even when the currently read
  projection already contains assignment context for the requested slice.
- `_workspace_log_redaction_context_bytes()` wraps an already guarded maximum
  with a redundant outer `max()`.

This repair is limited to those log-redaction hot paths. It does not change the
security contract for unknown leading assignment fragments, support bundles,
doctor output, service logs, or broader MCP behavior.

### Requirements Checklist

- Preserve byte-slice redaction behavior for UTF-8 text and overlapping secret
  spans.
- Avoid constructing an O(N) text-index byte-offset list for every
  secret-bearing byte-slice redaction.
- Skip assignment lookback when the current projection already contains a
  token-assignment value span covering the requested slice.
- Preserve assignment lookback for unknown leading fragments whose assignment
  prefix may predate the expanded read.
- Remove the redundant redaction-context `max()` without changing returned
  context sizes.

### Implementation Steps

1. Add a focused MCP regression showing visible assignment context does not
   trigger a second `read_log()` lookback.
2. Replace the full UTF-8 offset table with a targeted byte scanner that maps
   only redaction span endpoints.
3. Gate assignment lookback on whether the already-read leading fragment has a
   visible assignment value covering the caller slice.
4. Remove the redundant `_workspace_log_redaction_context_bytes()` outer
   `max()`.
5. Run focused tests and lint/type checks for the touched files only.
6. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence. Full AWF/GitHub validation remains owned by AWF after
   agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'visible_assignment_context or pattern_only_secret_assignment or assignment_lookback_failure or preserves_long_benign_token_without_assignment_context'
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k redact_secrets_byte_slice
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
```

## Review Thread `PRRT_kwDOSJAM6s6HC9ao` Overlapping Exact Secret Repair Plan

### Problem Statement And Scope

The review thread identifies that exact `extra_secrets` scanning advances the
search cursor to the end of each match. If an exact secret can overlap with
itself, later overlapping occurrences are not included in the merged redaction
spans, and arbitrary byte slices can expose the suffix of a second occurrence.

This repair is limited to exact configured-secret span discovery in
`src/awf/common/redaction.py` and a focused runtime regression. It does not
change provider token pattern definitions, MCP log-read projection, support
bundle behavior, or broader validation scope.

### Requirements Checklist

- Exact configured-secret discovery finds overlapping self-occurrences.
- Byte slices that intersect only a later overlapping occurrence are redacted.
- Existing non-overlapping configured-secret and pattern redaction behavior
  remains unchanged.

### Implementation Steps

1. Add a focused failing regression for a byte slice that intersects only a
   later overlapping occurrence of the same configured secret.
2. Update exact configured-secret scanning to continue from the next character
   after a match start so overlapping matches are discovered before span merge.
3. Run focused runtime redaction tests and lint/type checks for the touched
   files only.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence. Broad AWF/GitHub validation remains owned by AWF after
   agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k overlapping_exact_secret
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py tests/unit/runtime/test_log_redaction.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py
```
