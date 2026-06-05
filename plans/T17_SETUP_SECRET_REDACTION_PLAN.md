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
- Review thread `PRRT_kwDOSJAM6s6HE7vW` identified that followed service-log
  streaming decodes Docker log pipes with Python's default strict text mode,
  so invalid UTF-8 bytes can kill a redaction reader thread before later log
  lines are drained or written.
- This repair remains inside the existing T17 service-log redaction scope and
  only changes the followed subprocess decode policy plus a focused regression.
- Review-level comment `issue:4620175517` follow-up found the invalid-UTF-8
  streaming decode gap already fixed and covered, but identified two remaining
  review items: concurrent stdout/stderr broken-pipe handlers can both try to
  terminate the followed subprocess, and `redact_secrets` should document why
  exact-secret matching runs after regex masking while slice helpers compute
  spans on the original text.
- This repair remains inside the existing T17 redaction/service-log scope and
  only changes the broken-pipe termination guard, the redaction comment,
  focused regression, and this plan/validation evidence.
- Review-level comment `issue:4620175517` identified a remaining MCP log-read
  exact-secret gap for non-default deployments: `awf mcp serve --env-file` can
  start with a custom Compose env file, but `_workspace_log_redaction_provider_environ`
  still resolves provider secrets from the default local-service env file.
- This repair remains inside the existing T17 MCP log-redaction scope and only
  changes custom Compose env-file plumbing, focused regressions, and this
  plan/validation evidence.
- Review-level comment `issue:4620175517` identified one remaining followed
  service-log teardown gap: downstream write failures delivered as `OSError`
  or `ValueError` instead of `BrokenPipeError` can let the Docker follow
  subprocess continue running until it exits on its own.
- This repair keeps the MCP compose/env reread and lookback notes as
  non-blocking latency trade-offs, because current MCP code already short
  circuits visible assignment context and the remaining env-file caching concern
  is not a correctness or leak defect.
- Review-level comment `issue:4620175517` identified two final review notes:
  the short non-EOF MCP log offset projection is correct but needs a clarifying
  comment, and followed service-log broken-pipe teardown should give peer stream
  threads one more bounded join after the watchdog is stopped.
- This repair remains inside the existing T17 MCP/service-log scope and only
  changes the explanatory MCP offset comment, followed service-log teardown, a
  focused regression, and this plan/validation evidence.
- Review thread `PRRT_kwDOSJAM6s6HNslE` identified that direct
  `run_service_logs()` callers using default local-service compose discovery
  still default `compose_env_file` to explicit `None`, so exact-secret
  collection skips the adjacent `docker/compose/.env` provider credentials.
- This repair remains inside the existing T17 service-log redaction scope and
  only changes default service-log env-file resolution, a focused regression,
  and this plan/validation evidence.

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
- MCP workspace log exact-secret redaction honors the explicitly selected MCP
  Compose env file when the service is started with a custom `--env-file`.
- Captured and followed service-log output redact exact provider credential
  values loaded from the selected Compose env file, even when those values do
  not match token shape patterns and appear without an assignment or bearer
  prefix.
- Default `run_service_logs()` local-service compose discovery reads the
  adjacent Compose env file for exact-secret redaction while preserving explicit
  `compose_env_file=None` as no env file.
- Followed service-log streaming replaces invalid bytes before redaction so
  non-UTF-8 container output cannot terminate a stream reader.
- Followed service-log streaming terminates the followed subprocess at most
  once when both redaction threads observe downstream broken pipes.
- Followed service-log streaming terminates the followed subprocess when a
  downstream write or flush fails with `OSError` or `ValueError`, not only
  `BrokenPipeError`.
- Followed service-log broken-pipe teardown gives peer stream threads a final
  bounded drain opportunity after the blocked-write watchdog is stopped.
- Followed service-log streaming documents that the current per-line redaction
  boundary depends on single-line secret/provider-ref patterns.
- `redact_secrets` documents the deliberate post-regex exact-secret matching
  order and its relationship to original-text span helpers.
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
23. Add a focused regression for followed service logs containing invalid UTF-8
    bytes, confirm it fails, then set an explicit replacement decode policy on
    the followed subprocess pipes.
24. Verify the invalid-UTF-8 decode policy is already present, add a focused
    regression for simultaneous stdout/stderr broken pipes, then guard broken
    pipe termination so only the first stream performs subprocess cleanup.
25. Add a concise `redact_secrets` comment explaining why exact caller-supplied
    secrets are searched after regex substitutions in the full-text path while
    slice helpers derive all spans from the original text.
26. Add focused regressions for a custom MCP `--env-file` provider secret,
    confirm the default-only helper path misses it, then pass the selected
    Compose env file into MCP workspace-log exact-secret discovery.
27. Add a focused regression for followed service-log downstream `OSError` and
    `ValueError` during flush, confirm it leaves the process unterminated, then
    route those downstream write/flush failures through the existing
    broken-pipe cleanup path.

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

Review-thread `PRRT_kwDOSJAM6s6HE7vW` repair checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k replaces_invalid_bytes
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or replaces_invalid_bytes'
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HWEFg` MCP Multiline Compose Env Secret Plan

### Problem Statement And Scope

The review reports that MCP exact-secret collection reads the selected Compose
env file through `compose_env_file_values()`, whose current line-by-line parser
does not reconstruct Docker Compose quoted multiline env-file values. A
single-quoted provider secret such as a PEM body can therefore enter MCP text
artifacts or durable log reads with only the first physical line collected for
exact redaction.

This repair is limited to the MCP exact-secret collector, focused MCP
regressions, and this plan/validation evidence. It does not change branch
management, pushing, broad validation, full coverage, or unrelated Compose
parsing behavior.

### Requirements Checklist

- MCP exact-secret collection must reconstruct supported quoted multiline
  values from the selected Compose env file when the key is secret-like.
- MCP text artifact redaction must redact every line of a bare multiline
  Compose env-file provider secret.
- MCP workspace log reads must redact every line of the same bare multiline
  Compose env-file provider secret.
- Existing single-line Compose env-file redaction behavior must remain
  compatible.
- Run only focused MCP tests and narrow lint/type checks for touched files;
  leave broad AWF/GitHub validation and full coverage to AWF after agent
  completion.

### Implementation Steps

1. Add focused MCP artifact and workspace-log regressions in a new MCP test
   module proving a single-quoted multiline provider secret from the selected
   Compose env file is redacted in full.
2. Extend the MCP exact-secret collector with a small quoted multiline
   Compose-env reader that contributes only secret-key values and preserves the
   existing single-line parser path.
3. Deduplicate collected secrets through the existing `_mcp_secret_values()`
   return path.
4. Run the focused MCP regressions plus narrow lint/type checks for touched
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_multiline_compose_redaction.py -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_multiline_compose_redaction.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Review-Level Comment `issue:4620175517` Followed Service Logs Final Join Plan

### Problem Statement And Scope

The review-level comment reports that after a followed service-log broken pipe,
`_join_stream_threads` gives each redaction stream thread only one short timeout
before returning. A peer stream thread that already read a line from Docker can
briefly outlive teardown and write to stdout/stderr after the caller continues.

The same comment also notes that MCP short non-EOF workspace-log offset
projection is correct but subtle. This repair is limited to documenting that
offset decision and adding one extra bounded service-log stream-thread join
after the watchdog has been stopped. It does not change redaction rules,
subprocess command construction, MCP offset behavior, or broad validation
ownership.

### Requirements Checklist

- MCP short non-EOF `next_offset` projection has an in-code comment explaining
  why `eof=False` may require a follow-up poll at the covered offset.
- Followed service-log broken-pipe teardown stops the blocked-write watchdog
  before giving stream threads one more bounded join opportunity.
- The extra join remains bounded so a truly blocked downstream sink cannot hang
  the caller.
- Existing followed-log broken-pipe, blocked-write, and default streaming
  behavior remain covered by focused tests.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing regression where one followed stream triggers a broken
   pipe while the peer stream is still in a downstream write, then expect the
   peer to complete during the post-watchdog bounded join.
2. Add the MCP offset projection comment without changing return values.
3. Reuse the existing stream-thread tuple and add a final bounded join after
   `stream_watch_stop` is set and the watchdog thread has been joined.
4. Run the targeted regression, adjacent followed-log checks, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k peer_stream_after_watchdog_stop --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'peer_stream_after_watchdog_stop or broken_stdout_pipe or downstream_stdout_error or simultaneous_broken_pipes or blocked_downstream_write or default_follow_runner' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/mcp/metrics_tools.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py src/awf/mcp/metrics_tools.py
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

Review-level comment `issue:4620175517` custom MCP env-file checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k custom_compose_env_file_provider_secret
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q -k mcp_serve_runs_stdio_with_env_file
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py src/awf/mcp/server.py src/awf/cli/mcp_commands.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/cli/test_mcp_cli.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py src/awf/mcp/server.py src/awf/cli/mcp_commands.py
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

## Review-Level Comment `issue:4620175517` Streaming Follow-Up Plan

### Problem Statement And Scope

The review-level comment reported that followed service logs could crash on
non-UTF-8 Docker output, could terminate the streaming subprocess twice if both
stdout and stderr redaction threads observe broken pipes, and asked for a
maintainer note about the deliberate full-text/slice exact-secret matching
ordering in `awf.common.redaction`.

The invalid-byte decode policy is already present in the current checkout
(`encoding="utf-8", errors="replace"`) and has a focused regression. This
repair is limited to the remaining concurrent broken-pipe guard, the explanatory
redaction comment, focused tests/checks, and validation evidence.

### Requirements Checklist

- Preserve the existing followed service-log invalid-byte replacement behavior.
- Ensure simultaneous stdout/stderr broken-pipe callbacks terminate the
  streaming subprocess at most once.
- Preserve successful followed-log handling when only one stream breaks.
- Document why full-text `redact_secrets` runs exact-secret matching after
  regex masking while slice helpers compute all redaction spans from the
  original text.

### Implementation Steps

1. Add a focused failing regression where both followed-log stream sinks raise
   `BrokenPipeError` and the fake process fails if termination is attempted
   more than once.
2. Change the broken-pipe callback to let only the first thread terminate the
   subprocess.
3. Add a concise comment in `redact_secrets` explaining the post-regex
   exact-secret matching order and its equivalence with original-text span
   helpers.
4. Run focused pytest/ruff/mypy commands for the touched files only.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence. Broad AWF/GitHub validation remains owned by AWF after
   agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'simultaneous_broken_pipes or invalid_bytes'
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k 'redact_secrets_preserves_context or redact_secrets_byte_slice'
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/common/redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/runtime/test_log_redaction.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py src/awf/common/redaction.py
```

## Review Thread `PRRT_kwDOSJAM6s6HFLSV` Compose Secret-Key Parity Plan

### Problem Statement And Scope

The review thread reports that MCP workspace log reads collect exact Compose
secret values only for provider keys in `KNOWN_SECRET_ENV_KEYS`, while service
logs already treat broader secret-looking env names such as `*_SECRET`,
`*_PASSWORD`, and `*_API_KEY` as exact secrets. A bare Compose env value under
one of those broader keys can therefore be masked by `awf service logs` but
leak through `awf_read_workspace_log` if the value does not match token-shape
or assignment-pattern redaction.

This repair is limited to MCP workspace log exact-secret discovery, a focused
MCP regression, and this plan/validation evidence. It does not change token
pattern definitions, service-log behavior, support bundles, MCP log projection,
or broad AWF/GitHub validation scope.

### Requirements Checklist

- MCP workspace log exact-secret discovery includes local Compose env values
  whose keys match the same broad service secret-key convention as service
  logs.
- Bare non-pattern Compose secret values are redacted when an MCP caller reads
  a byte slice that overlaps the exact value.
- Existing provider-key exact redaction and pattern redaction behavior remains
  unchanged.

### Implementation Steps

1. Add a focused failing MCP regression with a Compose-only `*_SECRET` value
   that does not match token or assignment redaction patterns.
2. Update MCP exact-secret collection to use the broad service secret-key
   predicate already used by service logs.
3. Run the focused MCP regression and narrow lint/type checks for touched
   files only.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence. Broad AWF/GitHub validation remains owned by AWF after
   agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_custom_secret
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

## CI Repair Plan: `python-full-coverage` Cancellation

### Problem Statement And Scope

GitHub Actions run `26958642080` cancelled the `python-full-coverage` job at
the workflow's 60-minute timeout. The job reached 99% test progress, had
already printed pytest failure markers, and never reached pytest failure
reporting or coverage threshold evaluation. The required aggregate job failed
because `python-full-coverage` was `cancelled`; lint/type, console, and release
artifact jobs passed.

This repair is limited to the T17 setup-secret redaction branch. It will not
modify protected workflow gates, skip tests, reduce coverage requirements, or
run broad/full coverage locally inside the agent phase.

### Requirements Checklist

- Reproduce actionable pytest failures from the PR-touched test areas with
  focused commands.
- Fix real behavior or test bugs without weakening assertions or disabling CI.
- Remove avoidable T17 test/runtime overhead only when the covered behavior
  remains asserted by focused tests.
- Record focused verification evidence and state that broad AWF/GitHub
  validation remains owned by AWF after agent completion.

### Implementation Steps

1. Run focused pytest commands for PR-touched Python test files to expose the
   failure details hidden by the cancelled full-coverage job.
2. Inspect any slow T17 tests added by the branch and reduce unnecessary
   repetition or expensive setup while preserving behavioral assertions.
3. Implement the smallest source/test fixes required by those focused failures.
4. Run focused pytest, ruff, and mypy checks for touched files only.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence. Broad AWF/GitHub validation remains owned by AWF after
   agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest <focused changed tests> -q
uv run --python 3.12 --extra dev ruff check <touched source/test files>
uv run --python 3.12 --extra dev mypy <touched source files>
```

## CI Repair Plan: MCP Log Test Line Limit

### Problem Statement And Scope

GitHub Actions run `26962953418` completed full pytest execution on the
current head and failed `test_first_party_code_files_stay_under_line_limit`.
The oversized file is
`tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py` at 2,136
lines, over the 1,500-line first-party guard. The failure is a maintainability
test failure, not a redaction behavior failure or coverage-percentage miss.

This repair is limited to the owned MCP tests and plan/validation docs. It
will not weaken the line-limit guard, skip tests, change production behavior,
or run broad/full coverage locally inside the agent phase.

### Requirements Checklist

- Keep each first-party test file under the 1,500-line maintainability limit.
- Preserve the MCP workspace-log regression behavior and test names.
- Keep the split self-contained so pytest can collect the moved tests normally.
- Record focused verification evidence and leave broad AWF/GitHub full
  coverage to AWF/GitHub after agent completion.

### Implementation Steps

1. Move the complete `TestWorkspaceLogs` class from
   `test_mcp_server_part_003.py` into a new MCP server part file with the small
   fixture/helper header it needs.
2. Remove imports/helpers from part 003 that are only needed by the moved
   workspace-log tests.
3. Run focused collection/pytest and the line-limit guard for the touched test
   files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q -k line_limit
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
```

## Review Thread `PRRT_kwDOSJAM6s6HIJz7` Exact Secret Ordering Plan

### Problem Statement And Scope

The review thread reports that full-text `redact_secrets()` applies regex
redaction before exact caller-supplied `extra_secrets` matching. If an exact
secret contains a substring that regex redaction masks first, such as an HTTPS
URL with credentials or a full authorization header, the exact secret no longer
matches and non-regex portions of the configured secret can remain visible.

This repair is limited to shared full-text redaction ordering, a focused runtime
redaction regression, and validation evidence. Slice and byte-slice redaction
already compute spans on the original text and should keep their existing
contracts.

### Requirements Checklist

- Full-text `redact_secrets()` computes exact caller-supplied secret matches on
  the original input before rendering any regex redaction output.
- Exact configured secrets that contain URL-credential or authorization regex
  substrings are replaced as whole secrets.
- Regex-only redaction still preserves useful context such as
  `https://<redacted>@host` and `Authorization: Bearer <redacted>`.
- Slice and byte-slice redaction behavior remains unchanged.

### Implementation Steps

1. Add a focused failing regression with exact `extra_secrets` values that
   contain URL-credential and authorization-header regex substrings.
2. Change full-text redaction to compute and merge all redaction spans on the
   original text, then render the result once.
3. Preserve the URL `@` separator in regex-only full-text output while still
   masking the credential body.
4. Run focused pytest plus narrow lint/type checks for the touched files only.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k exact_secret
uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py tests/unit/runtime/test_log_redaction.py
uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py
```

## Review-Level Comment `issue:4620175517` MCP Lookback Exception Guard Plan

### Problem Statement And Scope

The review-level comment reports that
`_workspace_log_assignment_lookback_projection()` performs a secondary
`WorkspaceService.read_log()` call to recover assignment context, but lets
exceptions from that secondary read escape. The primary log read has already
succeeded at that point, so a transient lookback failure should degrade to the
existing conservative unknown-fragment fallback instead of failing the
`awf_read_workspace_log` MCP tool.

This repair is limited to the MCP workspace-log lookback path, a focused
regression in the existing MCP workspace-log tests, and validation evidence. It
does not change primary log-read error handling or broad validation ownership.

### Requirements Checklist

- Keep successful assignment lookback behavior unchanged.
- If the secondary assignment-lookback `read_log()` raises, return the
  conservative fallback tuple that treats the leading fragment as untrusted.
- Ensure the MCP tool still returns usable redacted data from the primary read
  instead of surfacing the secondary exception.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP regression where the primary log read succeeds and
   the secondary assignment-lookback read raises.
2. Guard the secondary lookback read and return the existing conservative
   `(result_text, projection_offset, True)` fallback on exception.
3. Run the targeted regression, the relevant MCP workspace-log test file, and
   narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k assignment_lookback_exception
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
```

## Review Thread `PRRT_kwDOSJAM6s6HJbA2` Inherited Service Env Secret Plan

### Problem Statement And Scope

The review thread reports that `awf service logs` omits inherited process
environment secrets from exact service-log redaction when `service_environ` is
omitted. In that default path Docker inherits `os.environ`, but
`_service_log_secret_values()` only scans the Compose env file and an explicit
mapping, so a bare non-pattern value from an exported secret-like variable can
appear in captured or followed service logs.

This repair is limited to inherited service-log exact-secret discovery, focused
captured/followed service-log regressions, and validation evidence. It does not
change MCP log reads, shared token patterns, Docker command construction, or
broad AWF/GitHub validation ownership.

### Requirements Checklist

- Captured service logs redact bare non-pattern values from inherited
  secret-like environment keys when `service_environ` is omitted.
- Followed service-log streams redact the same inherited exact secret values
  before writing to the operator terminal.
- Explicit `service_environ` and selected Compose env-file exact-secret
  redaction remain covered and unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add focused failing captured and followed service-log regressions using an
   inherited `ANTHROPIC_AUTH_TOKEN` value that does not match token patterns.
2. Include secret-like values from `os.environ` in `_service_log_secret_values()`
   along with selected Compose env-file values and any explicit service
   environment mapping.
3. Run the targeted service-log regressions, the adjacent Compose-env
   service-log redaction tests, and narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k inherited_env_secret --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'inherited_env_secret or compose_env_provider_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## CI Repair: Durable Log Coverage Reference Anchor

### Problem Statement And Scope

GitHub Actions run `26967929636` failed the full-coverage job at
`tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference`.
The registry still references
`tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams`,
but the prior line-limit repair moved all `TestWorkspaceLogs` tests into
part 005. The stale node breaks the executable coverage-reference contract.

This repair is limited to owned MCP test files and this plan/validation pair.
It will not weaken the registry smoke test, alter coverage-gate behavior, or
touch unowned protected quality-gate files.

### Requirements Checklist

- Restore the exact durable workspace-log pytest node referenced by the
  registry smoke contract.
- Preserve the behavior assertion for listing an indexed log stream and
  reading byte windows from it.
- Avoid duplicate copies of the same durable-log coverage test across MCP
  server part files.
- Keep touched MCP test files under the first-party line-limit guard.
- Record focused verification evidence and leave broad AWF/GitHub validation to
  AWF after agent completion.

### Implementation Steps

1. Move only `TestWorkspaceLogs.test_lists_and_reads_indexed_log_streams` from
   `test_mcp_server_part_005.py` back into
   `test_mcp_server_part_003.py`.
2. Add the minimal imports needed by that restored test in part 003 and remove
   any now-unused imports from part 005.
3. Run the focused failing pytest node, the registry smoke contract test, and
   focused lint for the touched MCP test files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with requirement
   status and evidence.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
```

## Review Thread `PRRT_kwDOSJAM6s6HKi5o` MCP Artifact Custom Secret Key Plan

### Problem Statement And Scope

The review thread reports that MCP safe payload and artifact exact-secret
redaction only treats Compose/provider environment entries as secrets when the
key is in `KNOWN_SECRET_ENV_KEYS`. MCP workspace log reads and service logs
already use the broader service secret-key predicate, so a non-token-shaped
value under a custom key such as `CUSTOM_CLIENT_SECRET` can be redacted in logs
but still appear in MCP artifact content or structured tool payloads.

This repair is limited to MCP payload/artifact exact-secret discovery, a
focused MCP artifact regression, and validation evidence. It does not change
the Compose env loader, shared token patterns, log-read redaction behavior, or
broad AWF/GitHub validation ownership.

### Requirements Checklist

- MCP artifact content redacts bare non-pattern values from custom
  secret-like Compose env-file keys.
- MCP payload/artifact exact-secret discovery uses the same service secret-key
  predicate already used by service logs and MCP workspace log reads.
- Existing known provider env-key redaction behavior remains unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP artifact regression using a custom
   `CUSTOM_CLIENT_SECRET` Compose env-file key with a bare non-token-shaped
   value.
2. Reuse the service-log secret-key predicate in `_mcp_secret_values()` when
   selecting exact Compose/provider env secrets.
3. Run the targeted regression, adjacent MCP artifact redaction tests, and
   narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k custom_compose_env_secret --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'compose_env_file_provider_secret or custom_compose_env_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HNslE` Service Log Default Env Sentinel Plan

### Problem Statement And Scope

The inline review reports that direct `run_service_logs()` callers using the
helper defaults still pass `compose_env_file=None`. That means the exact-secret
redaction context treats the call as explicit "no env file" and skips
provider secrets in the adjacent default `docker/compose/.env`, even though the
same helper resolves the default local-service compose file from the checkout.

This repair is limited to default service-log env-file resolution and exact
secret redaction for direct helper callers. It does not change CLI
path-verification behavior, explicit `compose_env_file=None`, MCP log
redaction, provider precedence, or broad validation ownership.

### Requirements Checklist

- Direct default `run_service_logs()` resolves the adjacent default Compose
  env file when the default local-service compose file is discovered.
- Bare provider secret values from that default env file are exact-redacted
  from captured service-log output.
- Explicit `compose_env_file=None` remains an explicit no-env-file choice.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing service-log regression where a nested caller relies on
   default compose discovery and the adjacent default `.env` contains a bare
   provider secret emitted by Docker logs.
2. Update `run_service_logs()` to use omitted env-file semantics by default and
   resolve that sentinel relative to the resolved default compose file.
3. Run the targeted regression, adjacent service-log redaction checks, and
   narrow ruff/mypy checks for touched files. Broad AWF/GitHub validation
   remains owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_default_resolves_adjacent_compose_env_file_for_redaction -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'default_resolves_adjacent_compose_env_file_for_redaction or redacts_compose_env_provider_secret or resolves_omitted_compose_env_file'
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review-Level Comment `issue:4620175517` Dead Wrapper And Secret-Key Helper Plan

### Problem Statement And Scope

The review-level comment reports two final cleanup issues in the current T17
redaction branch: `src/awf/runtime/logs.py` still contains an unreachable
sync `_read_log_chunk()` wrapper after the public async text read path was
refactored through `read_log_chunk_bytes()`, and MCP modules import the private
`_is_service_secret_env_key()` helper from `awf.service.logs`.

This repair is limited to removing the dead sync wrapper, promoting the
secret-env-key classifier to a public provider-readiness helper next to the
existing `KNOWN_SECRET_ENV_KEYS` constants, updating current call sites, adding
a focused classifier regression, and recording validation evidence. It does not
change redaction semantics, log window sizing, Compose env loading, or broad
AWF/GitHub validation ownership.

### Requirements Checklist

- `read_log_chunk()` continues to decode bytes returned by
  `read_log_chunk_bytes()` with replacement semantics.
- The unused private `_read_log_chunk()` sync wrapper is removed from
  `src/awf/runtime/logs.py`.
- Secret-env-key classification is exposed through a public helper in
  `awf.service.provider_readiness`.
- `awf.service.logs`, `awf.mcp.server`, and `awf.mcp.metrics_tools` no longer
  import `_is_service_secret_env_key()` across module boundaries.
- Existing service-log and MCP exact-secret selection behavior remains
  unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing provider-readiness regression for the public
   secret-env-key classifier.
2. Move the classifier implementation into `awf.service.provider_readiness`,
   update service-log and MCP imports/call sites, and remove the old private
   helper plus its now-local constants from `awf.service.logs`.
3. Remove the unreachable `_read_log_chunk()` wrapper.
4. Run the targeted classifier regression, adjacent service-log/MCP redaction
   checks, and narrow ruff/mypy checks for touched files.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k secret_env_key --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_logs.py -q -k 'read_uses_threaded_bounded_file_read or read_clamps_offsets' --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k 'short_secret_values or compose_env_provider_secret or custom_compose_env_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/runtime/logs.py src/awf/service/provider_readiness.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/logs.py src/awf/service/provider_readiness.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
```

## Review-Level Comment `issue:4620175517` Service-Log Short Secret Filter Plan

### Problem Statement And Scope

The review-level comment includes three follow-ups. Local code already aligns
MCP payload/artifact secret-key discovery with MCP log/service-log discovery by
using `is_secret_env_key()` in `_mcp_secret_values()`. The MCP
per-call Compose/env reread concern is a non-blocking latency trade-off already
kept outside this repair's correctness scope. The remaining actionable cleanup
is that `_service_log_secret_values()` collects short secret-like values before
the shared exact-secret redactor filters them later.

This repair is limited to filtering short exact-secret candidates in service-log
secret discovery, a focused helper regression, and validation evidence. It does
not change MCP log-read window sizing, introduce env-file caching, or alter
existing token/provider-ref redaction behavior.

### Requirements Checklist

- `_service_log_secret_values()` ignores secret-like values shorter than four
  characters from the selected Compose env file, inherited process environment,
  and explicit service environment mappings.
- Longer secret-like values from the same sources remain selected for exact
  service-log redaction.
- The MCP `_mcp_secret_values()` key-filter item is documented as stale because
  the local code already uses `is_secret_env_key()`.
- The MCP env-file caching item is documented as a deferred performance
  trade-off, not a correctness or leak fix in this comment cycle.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing regression for `_service_log_secret_values()` that
   includes short and long secret-like values from Compose, inherited, and
   explicit environment sources.
2. Add the minimum-length filter where service-log exact-secret candidates are
   collected.
3. Run the targeted helper regression, the adjacent service-log redaction
   checks, and narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k short_secret_values --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'short_secret_values or inherited_env_secret or compose_env_provider_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review Thread `PRRT_kwDOSJAM6s6HLG5x` MCP Artifact Unicode Secret Plan

### Problem Statement And Scope

The review thread reports that likely-text MCP artifacts decode bytes as
Latin-1 before applying exact `extra_secret_values` redaction. UTF-8 artifact
content containing a non-ASCII Compose/env-file secret such as
`p\u00e4ssw\u00f6rd1234` is transformed to mojibake before exact matching, so
the returned base64 payload can still include the original UTF-8 secret bytes.

This repair is limited to MCP artifact exact-secret redaction for text
artifacts, a focused MCP artifact regression, and validation evidence. It does
not change binary artifact blocking policy, MIME detection, Compose env loading,
or broad AWF/GitHub validation ownership.

### Requirements Checklist

- Likely-text UTF-8 MCP artifacts redact exact non-ASCII extra secrets before
  base64 content is returned.
- Binary artifact exact-secret blocking continues to detect configured secret
  bytes directly.
- Existing ASCII text artifact redaction behavior remains unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP artifact regression using a UTF-8 Compose env-file
   secret with non-ASCII characters.
2. Preserve exact secret bytes before the Latin-1 artifact text path, or decode
   text artifacts in a way that lets exact collected secret values match.
3. Run the targeted regression, adjacent MCP artifact redaction checks, and
   narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k unicode_compose_env_secret --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'compose_env_file_provider_secret or custom_compose_env_secret or unicode_compose_env_secret or binary_artifact_containing_compose_env_file_provider_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Review Thread `PRRT_kwDOSJAM6s6HLcza` MCP Artifact Overlapping Secret Bytes Plan

### Problem Statement And Scope

The review thread reports that MCP text-artifact exact-secret byte redaction
uses `bytes.replace()` before Latin-1 text redaction. `bytes.replace()` masks
only non-overlapping occurrences, so a configured exact secret that overlaps
with itself in artifact content, such as secret `abcabc` in `abcabcabc`, can
leave the tail of another complete occurrence visible in the returned base64
payload.

This repair is limited to exact configured-secret byte redaction in MCP
artifacts, a focused MCP artifact regression, and validation evidence. It does
not change binary artifact blocking policy, Compose env loading, MIME
detection, or broad AWF/GitHub validation ownership.

### Requirements Checklist

- Likely-text MCP artifacts redact overlapping configured exact-secret byte
  occurrences before base64 content is returned.
- Adjacent repeated exact-secret occurrences remain independently redacted so
  existing redaction-size and oversize behavior is preserved.
- Existing ASCII and non-ASCII exact-secret artifact redaction behavior remains
  unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP artifact regression using secret `abcabc` in
   artifact content `abcabcabc`.
2. Compute exact-secret byte spans on the original artifact bytes, merge true
   overlaps, and render the redacted byte content without relying on
   non-overlapping `bytes.replace()`.
3. Run the targeted regression, adjacent MCP artifact redaction checks, and
   narrow ruff/mypy checks for touched files.
4. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k overlapping_exact_secret_bytes --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'overlapping_exact_secret_bytes or redaction_expansion_triggers_oversized or unicode_compose_env_secret or octet_stream_without_null_bytes' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Review Thread `PRRT_kwDOSJAM6s6HMbkf` Multiline Follow Secret Plan

### Problem Statement And Scope

The review thread reports that `awf service logs --follow` redacts each
followed stdout/stderr line independently. If the selected Compose env file or
inherited service environment contains an exact configured secret with a
newline, no single streamed line contains the full secret value, so the secret
can be written to the operator terminal in pieces.

This repair is limited to followed service-log streaming exact-secret handling,
a focused service-log regression, and validation evidence. It does not change
non-follow captured logs, token pattern definitions, MCP log reads, or broad
AWF/GitHub validation ownership.

### Requirements Checklist

- Followed service-log streams keep enough per-pipe context to redact exact
  configured secrets that span newline boundaries.
- Followed service logs must not write partial fragments of a potential
  multiline exact secret before the stream proves they are ordinary output.
- Existing single-line exact-secret, token-pattern, invalid-byte, interrupt,
  and broken-pipe follow behavior remains unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add focused failing followed service-log regressions using a quoted
   multiline Compose env-file provider secret emitted across streamed lines,
   including overlapping multiline candidates and EOF after a potential prefix.
2. Buffer only the suffix that could still become a multiline exact secret and
   redact flushable chunks before writing them to the terminal.
3. Flush any remaining buffered stream text through the shared redactor at EOF.
4. Run the targeted regression, adjacent followed service-log checks, and
   narrow ruff/mypy checks for touched files.
5. Update `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md` with status and
   evidence. Broad AWF/GitHub validation remains owned by AWF after agent
   completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k multiline_compose_env_secret --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'multiline_compose_env_secret or overlapping_multiline_secret_candidates or flushes_multiline_secret_prefix_at_eof or follow_redacts_compose_env_provider_secret or default_follow_runner' --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review-Level Comment `issue:4620175517` MCP Log Startup Secret Cache Plan

### Problem Statement And Scope

The review-level comment reports that `awf_read_workspace_log` resolves service
settings and Compose/env-file redaction secrets on every log-read tool call.
That makes sustained polling re-read and re-parse the Compose env file, and it
is inconsistent with other MCP redaction surfaces that use the startup-time
secret tuple captured when the server is built.

This repair is limited to computing the workspace-log service settings and
extra secret tuple once during metrics-tool registration, then reusing that
tuple from the log-read closure. It does not change log redaction rules,
redaction context sizing, artifact redaction, or broad validation ownership.

### Requirements Checklist

- `awf_read_workspace_log` does not call `resolve_service_settings()` per log
  request.
- `awf_read_workspace_log` does not re-resolve Compose/env-file provider
  secrets per log request.
- Startup-time Compose/env-file provider secrets still redact exact bare values
  from workspace log slices.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP log regression that records service-settings and
   provider-env resolution counts after server build, then performs repeated
   `awf_read_workspace_log` calls and expects no additional resolution.
2. Capture `service_settings_value` and `extra_secret_values` once in
   `register_metrics_tools`, using injected startup values from
   `build_mcp_server` when available.
3. Replace the per-call log-read resolution with the captured secret tuple.
4. Run the targeted regression, adjacent MCP log redaction checks, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k startup_redaction_secrets --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k 'startup_redaction_secrets or compose_env_provider_secret or custom_compose_env_file_provider_secret' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HMFlI` Followed Service Logs Blocked Sink Plan

### Problem Statement And Scope

The inline review reports that `awf service logs --follow` can hang when the
redaction reader thread blocks writing to a downstream stdout/stderr pipe that
is full but still open. In that state the thread stops draining the subprocess
pipe, Docker can block on its own stdout/stderr pipe, and the main thread keeps
waiting on the followed process.

This repair is limited to followed service-log streaming. It does not change
non-follow log capture, redaction patterns, Docker Compose command construction,
or broad validation ownership.

### Requirements Checklist

- A blocked downstream followed-log write is detected and treated like a closed
  downstream pipe for process cleanup.
- The followed Docker logs subprocess is terminated once, rather than leaving
  `process.wait()` stuck behind a full subprocess pipe.
- Normal followed-log redaction and existing broken-pipe handling remain
  unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing regression where a followed stdout sink blocks without
   raising `BrokenPipeError`, and expect the default streaming runner to
   terminate the followed process.
2. Track active stream sink writes and add a short watchdog that terminates the
   followed process when a write remains blocked past the configured timeout.
3. Make blocked stream threads non-blocking for runner shutdown after the
   process has been terminated.
4. Run the targeted regression, adjacent followed-log tests, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k blocked_downstream_write --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'blocked_downstream_write or broken_stdout_pipe or downstream_stdout_error or simultaneous_broken_pipes or default_follow_runner' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review-Level Comment `4431520377` Support Bundle State Path Plan

### Problem Statement And Scope

The review-level comment reports that support-bundle `log_pointers` still
include the raw local `settings.work_dir` path. First-time evaluators may share
support bundles externally, so this can expose host usernames, checkout names,
or customer directory layout details. The existing setup-state summary already
reports only `work_dir_configured`, and `config_fingerprint` is also a
support-bundle surface, so this repair keeps those diagnostics useful without
embedding the absolute state path.

This repair is limited to support-bundle state-path privacy. It does not change
setup-state schema, service configuration, doctor/status diagnostics, log
collection commands, or broad AWF/GitHub validation ownership.

### Requirements Checklist

- Support-bundle `log_pointers` do not include the raw `settings.work_dir`
  value.
- Support-bundle `config_fingerprint` reports whether `work_dir` is configured
  without including the raw path.
- The state-directory pointer still indicates whether a state directory is
  configured.
- Existing service and worker log command pointers remain unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing support-bundle regression that uses a host-like
   `work_dir` path and asserts the serialized bundle omits it while preserving
   a configured/not-configured state pointer.
2. Replace the raw state-directory log pointer and support-bundle
   `config_fingerprint["work_dir"]` with configured/not-configured markers
   derived from `settings.work_dir`.
3. Run the targeted regression, adjacent support-bundle test subset, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k log_pointers_omit_work_dir --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k 'log_pointers_omit_work_dir or collects_required_sections or redacts_secrets' --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py tests/unit/service/test_support_bundle.py
uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HM_Mt` Private-Key Env Redaction Plan

### Problem Statement And Scope

The inline review reports that secret env-key classification omits conventional
private-key names such as `PRIVATE_KEY` and `SSH_PRIVATE_KEY`. Service-log and
MCP exact-secret collection use that classifier, so a private-key value from the
selected env sources can be missed when it appears as a bare log/artifact body.
The shared token-assignment regex also omits `PRIVATE_KEY`, so inline
`PRIVATE_KEY=value` text is not redacted like token/password assignments.

This repair is limited to private-key env-name classification and shared
assignment-style redaction. It does not change provider readiness probes,
service-log streaming mechanics, MCP log offset handling, or broad validation
ownership.

### Requirements Checklist

- `is_secret_env_key` returns true for exact `PRIVATE_KEY` names.
- `is_secret_env_key` returns true for suffix forms such as `SSH_PRIVATE_KEY`
  and hyphen-normalized equivalents.
- Shared assignment-style redaction masks `PRIVATE_KEY=value` and
  `SSH_PRIVATE_KEY=value` text.
- Existing non-secret classifier exclusions such as `PUBLIC_URL` and
  `TOKEN_BUCKET_SIZE` remain unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add focused failing regressions for private-key env classification and
   private-key assignment redaction.
2. Add `_PRIVATE_KEY` to the secret env-key suffix set so exact and suffixed
   names are classified consistently.
3. Add `PRIVATE[_-]?KEY` to the shared token-assignment key alternatives.
4. Run the targeted regressions, adjacent focused redaction checks, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_provider_readiness_public_secret_env_key_classifier tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py src/awf/common/token_patterns.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/runtime/test_log_redaction.py
uv run --python 3.12 --extra dev mypy src/awf/service/provider_readiness.py src/awf/common/token_patterns.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HNCRx` Service Log Env Sentinel Plan

### Problem Statement And Scope

The inline review reports that service-log exact-secret collection can pass the
public Compose env-file sentinel directly to `compose_env_file_values()`, whose
contract is `Path | None`. The default service-log call currently passes
`None`, but the helper still needs to tolerate `ComposeEnvFileInput` consistently
with adjacent service/MCP paths so the omitted sentinel resolves to the local
service Compose env file instead of reaching `Path.exists()`.

This repair is limited to resolving the service-log Compose env-file sentinel
before exact-secret collection and downstream logs command/env construction. It
does not change explicit `Path` or `None` behavior, provider readiness, MCP log
redaction, or broad validation ownership.

### Requirements Checklist

- `_service_log_secret_values()` accepts the public `ComposeEnvFileInput`.
- The omitted Compose env-file sentinel resolves to
  `LOCAL_SERVICE_COMPOSE_ENV_FILE` before parsing env-file values.
- `run_service_logs()` resolves the omitted sentinel before passing the env-file
  value to subprocess env and command construction.
- Explicit `Path` and `None` env-file behavior remains unchanged.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing regression that passes `COMPOSE_ENV_FILE_OMITTED` to
   `_service_log_secret_values()` and asserts a local-service env-file secret is
   collected without an `AttributeError`.
2. Add a focused public-entry regression for `run_service_logs()` that passes
   `COMPOSE_ENV_FILE_OMITTED`, redacts a local-service env-file secret, and
   proves the subprocess command receives the resolved env-file path.
3. Update the service-log helper type and resolve the omitted sentinel to
   `LOCAL_SERVICE_COMPOSE_ENV_FILE` before helper parsing and downstream
   `run_service_logs()` command/env construction.
4. Run the targeted regressions, adjacent focused service-log secret-value test,
   and narrow ruff/mypy checks for touched files. Broad AWF/GitHub validation
   remains owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_log_secret_values_resolves_omitted_compose_env_file tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_resolves_omitted_compose_env_file_before_subprocess tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_log_secret_values_skips_short_secret_values -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HNTqp` MCP Exact Extra-Secret Plan

### Problem Statement And Scope

The inline review reports that MCP structured JSON redaction applies
provider-readiness token/URL rewrites before matching exact extra secrets loaded
from Compose/provider environments. If a configured extra secret is an opaque
value that contains a token-shaped substring, `_safe_result` can return only
that substring redacted and leak the non-token prefix/suffix of the exact
secret.

This repair is limited to the MCP structured-payload text redaction order. It
does not change artifact byte-level redaction, MCP log redaction, service logs,
provider readiness snapshots, or broad validation ownership.

### Requirements Checklist

- MCP structured JSON redaction computes exact configured secret matches on the
  original text before provider-readiness token/URL rewrites.
- Compose-env-only provider secrets are redacted as a whole when they appear in
  `_safe_result` payloads.
- Existing provider token/URL pattern redaction remains in place for values
  that are not configured exact secrets.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP regression using a Compose env-file secret whose
   value contains a GitHub-token-shaped substring inside a structured event
   payload returned by `_safe_result`.
2. Update `_redact_sensitive_text()` so configured exact secrets are redacted
   before provider-readiness rewrites run.
3. Run the targeted regression, adjacent MCP event redaction checks, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceEvents::test_workspace_events_redact_exact_compose_secret_before_provider_rewrites -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HNhqB` MCP Shadowed Compose Env Secret Plan

### Problem Statement And Scope

The inline review reports that MCP exact-secret discovery reads provider
credentials from the merged Compose provider environment. When the selected
Compose env file and the MCP process environment define the same secret key,
the merged provider environment keeps only the process value. The raw env-file
value is still a configured secret and must be exact-redacted from MCP
structured payloads, artifacts, and workspace log reads.

This repair is limited to MCP startup exact-secret collection for the selected
Compose env file. It does not change provider environment precedence, service
logs, Compose runtime behavior, or broad validation ownership.

### Requirements Checklist

- MCP exact-secret discovery includes raw secret values parsed from the selected
  Compose env file before or in addition to merged provider environment values.
- A Compose env-file secret is exact-redacted even when the MCP process
  environment shadows the same secret key with a different value.
- Short values and non-secret env keys remain excluded by the existing secret
  key and minimum-length filters.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add a focused failing MCP workspace-log regression where
   `ANTHROPIC_AUTH_TOKEN` has one value in the selected Compose env file and a
   different value in the MCP process environment.
2. Update MCP secret collection to parse the selected Compose env file directly
   and include secret-key values before reading the merged provider environment.
3. Run the targeted regression, adjacent MCP log redaction tests, and narrow
   ruff/mypy checks for touched files. Broad AWF/GitHub validation remains owned
   by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py::TestWorkspaceLogs::test_read_workspace_log_redacts_shadowed_compose_env_file_provider_secret -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HNvc_` Multiline Private-Key Assignment Plan

### Problem Statement And Scope

The inline review reports that the shared token-assignment regex recognizes
`PRIVATE_KEY` keys but still captures only the first whitespace-delimited token
as the value. PEM-style assignments such as
`SSH_PRIVATE_KEY=<PEM private-key header>\n...` therefore redact only the first
header token and can leak the remaining key header/body through runtime
logs, audit text, and MCP log-slice assignment context.

This repair is limited to shared assignment-style redaction for PEM private-key
values and the MCP helper that uses the same named capture groups. It does not
change exact-secret discovery, provider refs, service-log subprocess behavior,
or broad validation ownership.

### Requirements Checklist

- Shared token-assignment matching treats PEM private-key assignment values as
  multiline values through the matching PEM private-key end marker.
- Runtime and audit text redaction mask the full PEM private-key assignment
  value while preserving the assignment key, separator, optional quotes, and
  surrounding non-secret text.
- MCP workspace-log assignment byte coverage treats bytes inside the multiline
  PEM value body as covered by the visible private-key assignment context.
- Existing single-token assignment matching remains compatible.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation to AWF after agent completion.

### Implementation Steps

1. Add focused failing regressions for shared runtime/audit private-key
   assignment redaction and MCP assignment byte coverage inside a PEM body.
2. Extend the shared assignment regex value branch with a PEM private-key
   multiline alternative while preserving the existing named groups.
3. Run the targeted regressions, adjacent token-pattern/log-redaction tests, and
   narrow ruff checks for touched files. Broad AWF/GitHub validation remains
   owned by AWF after agent completion.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py::test_shared_assignment_redactors_mask_multiline_private_key_values tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::test_workspace_log_assignment_value_covers_byte_inside_multiline_private_key -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::test_workspace_log_assignment_value_covers_byte_inside_multiline_private_key -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py tests/unit/common/test_token_patterns.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
```

## Review-Level Comment `issue:4620175517` MCP Byte-Window Integration Coverage Plan

### Problem Statement And Scope

The review-level comment found no remaining MCP byte-window defect but noted
that the dense projection/lookback logic would benefit from targeted integration
coverage against truncated and multibyte log files. Helper-level UTF-8 and
projection tests already exist, but the MCP tool integration surface should
also prove these edge cases through `awf_read_workspace_log`.

This repair is limited to focused MCP workspace-log tests and plan/validation
evidence. It does not change production redaction logic, service-log streaming,
support bundles, branch management, pushing, or broad validation ownership.

### Requirements Checklist

- `awf_read_workspace_log` clamps a requested byte window that extends past EOF
  in a multibyte log file and reports the correct `next_offset`, `eof`, and
  decoded data.
- `awf_read_workspace_log` preserves byte offsets when the requested caller
  window itself starts inside a multibyte UTF-8 character, returning replacement
  decoding for the truncated character byte without shifting later bytes.
- Existing MCP workspace-log redaction and assignment-lookback behavior remains
  unchanged.
- Run only focused tests and narrow lint checks for touched files; leave broad
  AWF/GitHub validation and full coverage to AWF after agent completion.

### Implementation Steps

1. Add focused MCP workspace-log tests in the existing `TestWorkspaceLogs`
   coverage file for a multibyte EOF-truncated window and a caller window that
   starts inside a multibyte UTF-8 character.
2. Run the new focused tests and narrow ruff check for the touched test file.
3. Update validation evidence with the focused checks and the no-production-code
   scope decision.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k 'truncated_multibyte_eof_window or requested_window_starts_inside_multibyte_character' --tb=short -ra
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
```

## Review-Level Comment `issue:4620175517` PEM Assignment Guard Plan

### Problem Statement And Scope

The review-level comment includes two service/redaction follow-ups. The
service-log helper contract is already narrowed in this checkout:
`_service_log_secret_values()` accepts `Path | None` and no longer resolves the
Compose env-file internally. The remaining actionable item is the shared
assignment regex's PEM private-key value branch, which should be explicitly
guarded so the multiline branch is only selected for values that start with a
PEM private-key header.

This repair is limited to `src/awf/common/token_patterns.py`, focused common
token-pattern tests, and this plan/validation evidence. It does not change
service-log subprocess behavior, MCP log projection, support bundles, branch
management, pushing, or broad validation ownership.

### Requirements Checklist

- Leave `_service_log_secret_values()` unchanged because it already accepts a
  resolved `Path | None` and has no internal re-resolution.
- Add an explicit lookahead guard to the PEM private-key value branch of the
  shared token-assignment regex.
- Preserve full multiline PEM private-key redaction and ordinary assignment
  redaction behavior.
- Run only focused tests and narrow lint for touched files; leave broad
  AWF/GitHub validation and full coverage to AWF after agent completion.

### Implementation Steps

1. Add a focused failing test that locks the PEM branch guard and verifies
   malformed PEM-like assignments still fall back to ordinary assignment
   matching without consuming following log text.
2. Add the lookahead guard to the PEM private-key branch in
   `TOKEN_ASSIGNMENT_PATTERN`.
3. Run the focused token-pattern tests and narrow ruff check for touched files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py::test_token_assignment_pattern_guards_multiline_private_key_branch -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py::test_token_assignment_pattern_guards_multiline_private_key_branch tests/unit/common/test_token_patterns.py::test_shared_assignment_redactors_mask_multiline_private_key_values tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py tests/unit/common/test_token_patterns.py
```

## PR 391 CI Failure Repair Plan

### Problem Statement And Scope

GitHub Actions CI run `26983719773` failed in `python-full-coverage` because
four tests failed while total coverage still exceeded the required threshold:

- `TestReadWorkspaceArtifact::test_redaction_expansion_triggers_oversized`
  expected an oversized post-redaction artifact, but its adjacent repeated
  secret bytes are correctly coalesced into one redaction marker and shrink.
- MCP/docs surface-introspection tests pass `MagicMock()` as settings, but
  `build_mcp_server()` now resolves service settings and needs a real
  `Settings` object for that path.
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py` is 1510
  lines, over the first-party file limit.

This repair is limited to test correctness, test placement, and focused
verification. It does not change broad validation ownership, branch
management, pushing, workflow files, quality gates, or unrelated source logic.

### Requirements Checklist

- The oversized artifact regression must use input whose redacted content
  expands past `limit_bytes` because separate secret spans are replaced by
  separate redaction markers.
- MCP/docs surface-introspection tests must construct the MCP server with real
  `Settings(_env_file=None)` instead of mock settings.
- `test_mcp_server_part_004.py` must fall back under the configured line limit
  without deleting behavioral coverage.
- The previously failing four-test focused repro must pass.
- Run only focused tests and narrow lint checks for touched files; leave broad
  AWF/GitHub validation and full coverage to AWF after agent completion.

### Implementation Steps

1. Move the oversized artifact regression out of
   `test_mcp_server_part_004.py` into the existing adjacent MCP part file so
   part 004 is below the line limit.
2. Update the moved regression payload to include separators between exact
   secret occurrences and assert the resulting expanded redacted byte length.
3. Replace bare mock MCP settings in the surface-introspection tests with
   `Settings(_env_file=None)`.
4. Re-run the four-test focused repro plus targeted adjacent tests and narrow
   lint for touched tests.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py::TestReadWorkspaceArtifact::test_redaction_expansion_triggers_oversized tests/unit/mcp/test_mcp_client_parity_docs.py::test_parity_matrix_matches_real_surfaces tests/unit/docs/test_pr_monitor_adoption_docs.py::test_adoption_docs_publish_real_rest_cli_and_mcp_names tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py tests/unit/mcp/test_mcp_client_parity_docs.py::test_parity_matrix_matches_real_surfaces tests/unit/docs/test_pr_monitor_adoption_docs.py::test_adoption_docs_publish_real_rest_cli_and_mcp_names -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/docs/test_pr_monitor_adoption_docs.py
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HV86k` Multiline Compose Env Secret Plan

### Problem Statement And Scope

The review reports that service-log exact-secret collection reads the selected
Compose env file through a parser that handles each physical line separately.
Docker Compose supports single-quoted multiline env-file values, so a provider
secret such as a PEM key can be collected as only its first line. That leaves
later lines available to leak from captured or followed service logs.

This repair keeps the change at the review target: service-log exact-secret
collection. It is limited to reconstructing quoted multiline Compose env-file
values for service-log redaction, a focused service-log regression, and this
plan/validation evidence. It does not change branch management, pushing, broad
validation, full coverage, or unrelated Compose parsing behavior.

### Requirements Checklist

- Service-log exact-secret collection must reconstruct Docker Compose
  single-quoted multiline values from the selected Compose env file, preserving
  embedded newlines and existing escaped-quote behavior for redaction inputs.
- Service-log exact-secret collection must include the full multiline provider
  secret from the selected Compose env file so captured bare log output cannot
  expose any fragment.
- Existing single-line Compose env-file parsing and service-log redaction
  behavior must remain compatible.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation and full coverage to AWF after agent completion.

### Implementation Steps

1. Add a focused service-log regression proving captured bare output redacts
   every fragment of a single-quoted multiline provider secret.
2. Add a small service-log redaction-only helper that reads quoted multiline
   entries from the selected Compose env file and contributes only
   secret-key values to the existing exact-secret list.
3. Keep the existing `compose_env_file_values()` path for normal one-line
   Compose env values and deduplicate any overlapping redaction candidates.
4. Run the focused service-log tests plus narrow lint/type checks for touched
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_redacts_single_quoted_multiline_compose_env_secret_from_captured_output -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_redacts_single_quoted_multiline_compose_env_secret_from_captured_output tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_redacts_compose_env_provider_secret_from_captured_output tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_follow_redacts_multiline_compose_env_secret_from_streamed_output -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
```

## Review-Level Comment `issue:4620175517` Exact Byte API Token And Quoted PEM Whitespace Plan

### Problem Statement And Scope

The review-level comment reports two remaining redaction fragility issues:
MCP byte-level exact redaction omits `service_settings.api_token` from its
explicit configured-secret set, and the shared token-assignment regex handles
quoted PEM private-key assignments only when the closing quote immediately
follows the PEM footer. A quoted PEM value with whitespace before the closing
quote can therefore fail to redact as a complete multiline assignment.

This repair is limited to MCP exact byte-secret collection,
assignment-pattern quoted PEM handling, focused regressions, and this
plan/validation evidence. It does not change branch management, pushing,
broad validation, full coverage, or unrelated redaction surfaces.

### Requirements Checklist

- `_redact_exact_secret_bytes()` must redact `service_settings.api_token`
  even when `extra_secrets` is incomplete.
- Quoted PEM private-key assignments must redact the full multiline value when
  whitespace appears between the PEM footer and the closing quote.
- Unquoted PEM private-key assignments must not consume the following log
  newline solely to satisfy the quoted-whitespace case.
- Existing assignment redaction output for quoted and unquoted ordinary values
  remains compatible.
- Run only focused tests and narrow lint/type checks for touched files; leave
  broad AWF/GitHub validation and full coverage to AWF after agent completion.

### Implementation Steps

1. Add focused failing regressions for service API-token byte redaction and a
   quoted PEM private-key assignment with trailing whitespace before the closing
   quote.
2. Add `service_settings.api_token` to the MCP byte exact-secret set.
3. Update the shared token-assignment quote handling so quoted values can have
   whitespace before the matching closing quote without broadening unquoted PEM
   matching.
4. Run the focused regressions, adjacent token-pattern/log-redaction checks,
   and narrow lint/type checks for touched files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_redaction_helpers.py::test_redact_exact_secret_bytes_includes_service_api_token_without_extra_secret tests/unit/common/test_token_patterns.py::test_shared_assignment_redactors_mask_quoted_pem_private_key_with_trailing_whitespace -q --tb=short -ra
uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py::test_shared_assignment_redactors_mask_quoted_pem_private_key_with_trailing_whitespace tests/unit/common/test_token_patterns.py::test_shared_assignment_redactors_mask_multiline_private_key_values tests/unit/common/test_token_patterns.py::test_token_assignment_pattern_guards_multiline_private_key_branch tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values tests/unit/mcp/test_mcp_server_redaction_helpers.py::test_redact_exact_secret_bytes_includes_service_api_token_without_extra_secret -q --tb=short -ra
uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py src/awf/common/token_patterns.py src/awf/common/audit.py tests/unit/mcp/test_mcp_server_redaction_helpers.py tests/unit/common/test_token_patterns.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py src/awf/common/token_patterns.py src/awf/common/audit.py
```
