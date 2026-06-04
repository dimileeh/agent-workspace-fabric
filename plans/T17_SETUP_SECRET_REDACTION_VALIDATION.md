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
- Complete: MCP workspace log reads redact with enough surrounding context that
  arbitrary requested offsets cannot reveal substrings of configured secrets.
- Complete: MCP workspace log reads preserve requested byte offsets when
  redaction context expansion starts inside a multibyte UTF-8 character.
- Complete: MCP workspace log reads do not expose pattern-only secret
  assignment values when the assignment key prefix is outside the fixed context
  window.
- Complete: MCP workspace log assignment-context early-break logic compares
  byte offsets to byte offsets when multibyte text appears before an
  assignment.
- Complete: MCP workspace log exact-secret redaction includes provider
  credentials loaded from the local Compose env file when they are absent from
  the MCP process environment.
- Complete: MCP workspace log exact-secret redaction honors the explicitly
  selected MCP Compose env file when the service is started with a custom
  `--env-file`.
- Complete: Captured and followed service-log output redact exact provider
  credential values loaded from the selected Compose env file when the log emits
  the bare value without an assignment or bearer prefix.
- Complete: Followed service-log streaming replaces invalid bytes before
  redaction so non-UTF-8 container output cannot terminate a stream reader.
- Complete: MCP workspace log reads do not skip data when the expanded log read
  is short without EOF; `next_offset` advances only through bytes actually
  covered by the expanded result.
- Complete: Followed service-log streaming terminates the followed subprocess
  when downstream write or flush failures are delivered as `OSError` or
  `ValueError`, not only `BrokenPipeError`.
- Complete: MCP workspace log reads mask an unknown leading value fragment when
  assignment lookback cannot read enough context to prove the fragment safe.
- Complete: Support-bundle setup-state collection returns a redacted failed
  setup-state payload if loaded config summarization raises after the config
  reader succeeds.
- Complete: Support-bundle setup-state generic fallback reason codes are
  centralized.
- Complete: Followed service-log streaming documents that the current per-line
  redaction boundary depends on single-line secret/provider-ref patterns.
- Complete: MCP binary secret detection documents why service-side token/URL
  regexes are retained after the shared redaction guard.
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

## Review Thread `PRRT_kwDOSJAM6s6G_-Om` Iteration

Additional files changed:

- `src/awf/common/redaction.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/runtime/test_log_redaction.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# failed during collection: ImportError for missing redact_secrets_slice after adding the regression
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# 47 passed

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 2 source files
```

## Review Thread `PRRT_kwDOSJAM6s6HABmr` Iteration

Additional files changed:

- `src/awf/service/support_bundle.py`
- `tests/unit/service/test_support_bundle.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_unexpected_config_reader_errors
# failed: ConfigReaderError escaped _setup_state and aborted collect_support_bundle
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k 'setup_state_degrades_unexpected_config_reader_errors or setup_state_redacts_config_load_errors'
# 2 passed, 16 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py tests/unit/service/test_support_bundle.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py
# Success: no issues found in 1 source file
```

## Review Thread `PRRT_kwDOSJAM6s6HAjVz` Iteration

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k pattern_only_secret_assignment
# failed: returned raw "deep-secret-fragment" from a long SERVICE_TOKEN= value
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k pattern_only_secret_assignment
# 1 passed, 25 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# 26 passed

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

## Review-Level Comment `issue:4620175517` Iteration

Additional files changed:

- `src/awf/service/support_bundle.py`
- `src/awf/mcp/server.py`
- `tests/unit/service/test_support_bundle.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_loaded_config_summary_errors
# failed: RuntimeError escaped _setup_state while summarizing source-checkout marker_count
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k 'setup_state_degrades_loaded_config_summary_errors or setup_state_degrades_unexpected_config_reader_errors or setup_state_redacts_config_load_errors'
# 3 passed, 16 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py src/awf/mcp/server.py tests/unit/service/test_support_bundle.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py src/awf/mcp/server.py
# Success: no issues found in 2 source files
```

## Review Thread `PRRT_kwDOSJAM6s6HBBcY` Iteration

Additional files changed:

- `src/awf/runtime/logs.py`
- `src/awf/service/workspaces.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k expanded_context_starts_inside_multibyte_character
# failed: returned "<redacted> TARG" after replacement-decoded bytes shifted the requested window
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k expanded_context_starts_inside_multibyte_character
# 1 passed, 26 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# 27 passed

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_logs.py -q -k read_clamps_offsets_and_zero_limits
# 1 passed, 25 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py -q -k read_log_rejects_missing_and_out_of_root_streams_then_reads_chunk
# 1 passed, 59 deselected

uv run --python 3.12 --extra dev ruff check src/awf/runtime/logs.py src/awf/service/workspaces.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/runtime/logs.py src/awf/service/workspaces.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 3 source files
```

## Review-Level Comment `issue:4620175517` Short-Read Iteration

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k short_non_eof_expanded_read
# failed: returned next_offset=15 for a short non-EOF expanded read that only covered through byte 8
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k short_non_eof_expanded_read
# 1 passed, 27 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# 28 passed

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

## Review Thread `PRRT_kwDOSJAM6s6HBsS0` Iteration

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_failure
# failed: returned raw "leaking-assignment-tail" when assignment lookback was one byte short
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_failure
# 1 passed, 31 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'assignment_lookback_failure or pattern_only_secret_assignment or preserves_long_benign_token_without_assignment_context'
# 3 passed, 29 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Collision/Encoding Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: setup-state output preserves every configured provider/client when
  multiple raw names redact to the same display key, using stable `#2` suffixes
  only for colliding redacted keys.
- Complete: raw provider/client names are absent from the setup-state collision
  regression output.
- Complete: non-colliding setup-state shape is preserved by the existing
  setup-state tests.
- Complete: `_unknown_leading_log_value_fragment_end` checks the first
  character before scanning and no longer encodes the whole expanded text for
  the delimiter fast path.
- Complete: existing MCP workspace-log offset/redaction behavior remains
  covered by the focused workspace-log subset.

Additional files changed:

- `src/awf/service/support_bundle.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/service/test_support_bundle.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing checks before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_preserves_redacted_name_collisions
# failed: only one `<redacted>` provider/client entry remained after dict overwrite

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k unknown_leading_log_value_fragment_end
# failed: `_unknown_leading_log_value_fragment_end` called whole-string encode
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_preserves_redacted_name_collisions
# 1 passed, 19 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k unknown_leading_log_value_fragment_end
# 2 passed, 29 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state
# 5 passed, 15 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'unknown_leading_log_value_fragment_end or read_workspace_log'
# 9 passed, 22 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py src/awf/mcp/metrics_tools.py tests/unit/service/test_support_bundle.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 2 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HBsyZ` Followed Service Logs Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: followed service logs no longer let the default subprocess runner
  write raw Docker output directly to the terminal; the streaming path pipes
  stdout/stderr through `redact_secrets()` before writing.
- Complete: followed service logs preserve streaming behavior by redacting and
  flushing output line by line while the process runs.
- Complete: follow interrupt return codes and `KeyboardInterrupt` behavior
  remain covered by the focused CLI/service tests.
- Complete: non-follow captured log behavior remains covered by the existing
  default runner and captured-output tests.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k default_follow_runner_redacts_streamed_output
# failed: raw ghp token and plain-file secret ref were captured from the direct subprocess output
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or default_subprocess_runner'
# 8 passed, 9 deselected

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py -q -k service_logs_follow
# 4 passed, 35 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Log Redaction Performance Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: byte-slice redaction behavior for UTF-8 text and overlapping
  secret spans remains covered by the focused runtime redaction tests.
- Complete: `redact_secrets_byte_slice()` no longer builds a full
  text-index-to-byte-offset list; it maps only requested redaction span
  endpoints by scanning the encoded UTF-8 bytes.
- Complete: MCP workspace log reads skip assignment lookback when the current
  projection already contains an assignment value covering the requested slice.
- Complete: MCP workspace log reads still use lookback for unknown leading
  fragments whose assignment prefix may predate the expanded read, and still
  mask failed lookback fragments.
- Complete: `_workspace_log_redaction_context_bytes()` no longer has the
  redundant outer `max()`.

Additional files changed:

- `src/awf/common/redaction.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/runtime/test_log_redaction.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k visible_assignment_context
# failed: `awf_read_workspace_log` issued a second `read_log()` call even though the expanded projection already contained `SERVICE_TOKEN=` context
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k visible_assignment_context
# 1 passed, 33 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'visible_assignment_context or pattern_only_secret_assignment or assignment_lookback_failure or preserves_long_benign_token_without_assignment_context'
# 4 passed, 30 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k redact_secrets_byte_slice
# 3 passed, 23 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'unknown_leading_log_value_fragment_end or read_workspace_log'
# 11 passed, 23 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q
# 26 passed

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/runtime/test_log_redaction.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 2 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HCaLj` Followed Service Logs Interrupt Cleanup Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: followed service-log interrupts at the default streaming
  `process.wait()` path terminate the Docker child before
  `run_service_logs()` returns.
- Complete: cleanup escalates to `kill()` and reaps the child when graceful
  termination times out.
- Complete: stdout/stderr redaction reader threads are joined through the
  interrupt cleanup path.
- Complete: `run_service_logs(follow=True)` still reports an empty successful
  result for `KeyboardInterrupt`.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k reaps_default_process
# failed: the interrupted default follow process returned through run_service_logs() without calling terminate()
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k reaps_default_process
# 2 passed, 17 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or default_subprocess_runner'
# 10 passed, 9 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HCoIm` Invalid UTF-8 Offset Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: MCP workspace log reads preserve requested byte offsets when
  invalid UTF-8 bytes appear in expanded redaction context before the requested
  window.
- Complete: byte-window rendering still returns replacement-decoded text for
  invalid raw bytes while keeping secret redaction byte offsets aligned to the
  original durable log bytes.
- Complete: existing MCP multibyte-boundary and configured-secret byte-slice
  regressions remain covered by the focused subset.

Additional files changed:

- `src/awf/common/redaction.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k invalid_utf8_before_requested_window
# failed: returned "x TARG" after replacement-decoded bytes shifted the requested window
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k invalid_utf8_before_requested_window
# 1 passed, 34 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'invalid_utf8_before_requested_window or expanded_context_starts_inside_multibyte_character or read_workspace_log_redacts_slice_starting_inside_configured_secret'
# 3 passed, 32 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k redact_secrets_byte_slice
# 3 passed, 23 deselected

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 2 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HC827` Assignment Lookback Trust Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: MCP workspace log reads no longer clear unknown-leading-fragment
  masking merely because assignment lookback covers the requested byte window.
- Complete: a widened lookback projection must show either visible assignment
  coverage for the requested slice or no unknown leading token fragment before
  the fragment is treated as safe.
- Complete: prior failed-lookback masking, pattern-only assignment masking, and
  benign-token preservation remain covered by focused MCP regressions.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_still_mid_fragment
# failed: returned raw "still-leaking-assignment-tail" after a covering lookback that still started mid-token
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_lookback_still_mid_fragment
# 1 passed, 36 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'assignment_lookback_still_mid_fragment or assignment_lookback_failure or pattern_only_secret_assignment or preserves_long_benign_token_without_assignment_context'
# 4 passed, 33 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HC9ao` Overlapping Exact Secret Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: exact configured-secret discovery now finds overlapping
  self-occurrences by advancing to the next character after a match start.
- Complete: a byte slice that intersects only a later overlapping configured
  secret occurrence is redacted.
- Complete: focused slice and byte-slice regressions still pass for the touched
  redaction helper.

Additional files changed:

- `src/awf/common/redaction.py`
- `tests/unit/runtime/test_log_redaction.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k overlapping_exact_secret
# failed: returned raw "abc" for byte slice 6:9 in "abcabcabc" with extra secret "abcabc"
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k 'redact_secrets_slice or redact_secrets_byte_slice'
# 8 passed, 19 deselected

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py tests/unit/runtime/test_log_redaction.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Byte-Break/Streaming-Boundary/Reason-Code Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: `_workspace_log_assignment_value_covers_byte()` now computes the
  assignment value byte start before applying the early-break optimization, so
  multibyte text before an assignment cannot mix character and byte indexes.
- Complete: followed service-log streaming keeps line-by-line redaction and
  documents that future multiline secret patterns would need carry-over
  context.
- Complete: setup-state generic read and summary fallback reason codes are
  shared constants, with the no-`reason_code` reader fallback covered.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `src/awf/service/logs.py`
- `src/awf/service/support_bundle.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `tests/unit/service/test_support_bundle.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing checks before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_value_covers_byte_breaks_using_byte_offsets
# failed: byte/character early-break mismatch continued to a later synthetic match

uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_unexpected_config_reader_errors_without_reason_code
# failed: shared reader fallback reason constant did not exist yet
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k assignment_value_covers_byte_breaks_using_byte_offsets
# 1 passed, 37 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state_degrades_unexpected_config_reader_errors_without_reason_code
# 1 passed, 20 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q -k setup_state
# 6 passed, 15 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'assignment_value_covers_byte or visible_assignment_context or read_workspace_log_skips_lookback_when_visible_assignment_context'
# 3 passed, 35 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py src/awf/service/logs.py src/awf/service/support_bundle.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/service/test_support_bundle.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py src/awf/service/logs.py src/awf/service/support_bundle.py
# Success: no issues found in 3 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HDTtb` Compose Env Provider Secret Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: MCP workspace log exact-secret discovery now resolves the same
  local service provider environment used by service readiness/status surfaces.
- Complete: provider credentials supplied only by the local Compose env file are
  included in the exact-secret set for context-aware log slice redaction.
- Complete: a requested MCP log slice containing only the bare Compose-sourced
  provider token, without a visible `TOKEN=` assignment prefix, is redacted.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_provider_secret
# failed: returned raw "compose-only-anthropic-provider-secret"
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_provider_secret
# 1 passed, 38 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q
# 39 passed

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HDiER` Service Log Compose Env Secret Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: captured service-log stdout/stderr redacts exact provider credential
  values parsed from the selected Compose env file.
- Complete: if the resolved service environment overrides a secret key from the
  selected Compose env file, both the env-file value and resolved value are
  redacted.
- Complete: followed service-log streaming passes the same exact provider
  credential values into the streaming redactor before writing to the terminal.
- Complete: non-secret Compose env values such as `COMPOSE_PROJECT_NAME` remain
  visible in service-log output.

Additional files changed:

- `src/awf/common/redaction.py`
- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k compose_env_provider_secret
# 2 failed, 19 deselected: both captured and followed service-log output returned the raw Compose-only provider secret.
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k compose_env_provider_secret
# 2 passed, 19 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'redact_secrets or compose_env_provider_secret'
# 29 passed, 19 deselected

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py src/awf/service/logs.py
# Success: no issues found in 2 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HE7vW` Follow Decode Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: followed service-log subprocess pipes now decode invalid bytes with
  replacement before the redaction stream threads consume lines.
- Complete: stdout and stderr streaming both continue when a container emits
  non-UTF-8 bytes.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k replaces_invalid_bytes
# failed: stdout/stderr reader threads raised UnicodeDecodeError and wrote no stream output.
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k replaces_invalid_bytes
# 1 passed, 22 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'follow or replaces_invalid_bytes'
# 12 passed, 11 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Streaming Follow-Up Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: the reported invalid-byte decode gap was already fixed in the
  current checkout with `encoding="utf-8"` and `errors="replace"` on the
  followed service-log `Popen` call; the existing invalid-byte regression still
  passes.
- Complete: simultaneous stdout/stderr broken-pipe callbacks now terminate the
  followed subprocess at most once.
- Complete: existing followed-log broken-pipe and invalid-byte handling remains
  covered by focused service-log tests.
- Complete: `redact_secrets` now documents why full-text exact-secret matching
  runs after regex masking while slice helpers compute spans from the original
  text.

Additional files changed:

- `src/awf/common/redaction.py`
- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k simultaneous_broken_pipes
# failed: terminate_count was 2 when both stream threads hit BrokenPipeError
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'simultaneous_broken_pipes or invalid_bytes'
# 2 passed, 22 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k 'redact_secrets_preserves_context or redact_secrets_byte_slice'
# 5 passed, 22 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q
# 24 passed

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/common/redaction.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/runtime/test_log_redaction.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py src/awf/common/redaction.py
# Success: no issues found in 2 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HFLSV` Compose Secret-Key Parity Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: MCP workspace log exact-secret discovery now uses the same broad
  service secret-key predicate as service logs for local Compose env values.
- Complete: a Compose-only `CUSTOM_CLIENT_SECRET` value that does not match
  token-shape or assignment-pattern redaction is masked when read through an
  overlapping MCP byte slice.
- Complete: existing Compose provider-key exact redaction remains covered by
  the adjacent focused regression.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_custom_secret
# failed: returned raw "bare-compose-custom-value" from MCP workspace log read
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k compose_env_custom_secret
# 1 passed, 39 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'compose_env_provider_secret or compose_env_custom_secret'
# 2 passed, 38 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Custom MCP Env-File Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: `_workspace_log_redaction_provider_environ` now accepts caller
  compose inputs and resolves exact provider secrets from the selected Compose
  env file instead of always using the default local-service env file.
- Complete: `build_mcp_server` and metrics tool registration carry the selected
  env file into `awf_read_workspace_log`.
- Complete: `awf mcp serve --env-file` passes the resolved env-file path to the
  MCP server factory while omitted env-file startup preserves default discovery
  semantics.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `src/awf/mcp/server.py`
- `src/awf/cli/mcp_commands.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `tests/unit/cli/test_mcp_cli.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing checks before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k custom_compose_env_file_provider_secret
# failed: build_mcp_server() got an unexpected keyword argument 'compose_env_file'

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q -k mcp_serve_runs_stdio_with_env_file
# failed: build_mcp_server test double did not receive required compose_env_file
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k custom_compose_env_file_provider_secret
# 1 passed, 40 deselected

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q -k mcp_serve_runs_stdio_with_env_file
# 1 passed, 6 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'compose_env_provider_secret or compose_env_custom_secret or custom_compose_env_file_provider_secret'
# 3 passed, 38 deselected

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py -q -k 'mcp_serve_runs_stdio_with_env_file or mcp_serve_runs_stdio_without_env_file'
# 2 passed, 5 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py src/awf/mcp/server.py src/awf/cli/mcp_commands.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/cli/test_mcp_cli.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py src/awf/mcp/server.py src/awf/cli/mcp_commands.py
# Success: no issues found in 3 source files
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` Downstream Write Failure Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: followed service-log streaming now routes downstream
  `OSError`/`ValueError` write or flush failures through the existing
  broken-pipe cleanup path.
- Complete: the followed subprocess is terminated and the stream result is
  treated as an empty successful interrupt-style result when the downstream
  sink is unavailable.
- Complete: existing broken-pipe and simultaneous broken-pipe teardown behavior
  remains covered by the adjacent focused tests.

Scope note: MCP compose/env-file reread caching remains a non-blocking latency
trade-off because it is not a correctness or redaction leak defect in this
review cycle.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k oserror_stdout_pipe
# failed: OSError escaped the stream thread and the followed process was not terminated
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k downstream_stdout_error
# 2 passed, 24 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'downstream_stdout_error or broken_stdout_pipe or simultaneous_broken_pipes or default_follow_runner'
# 6 passed, 20 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## CI Repair: `python-full-coverage` Cancellation Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: GitHub Actions run `26958642080` was inspected. The aggregate
  `ci-required` job failed because `python-full-coverage` was cancelled at the
  60-minute workflow timeout while lint/type, console, and release-artifacts
  succeeded.
- Complete: Focused local pytest reproduced the hidden actionable failures in
  PR-touched tests. The three failing MCP workspace-log tests hard-coded
  `_LOG_REDACTION_CONTEXT_BYTES` as the first expanded read context even though
  MCP exact-secret discovery can increase that context from default Compose
  secret-like values.
- Complete: The MCP lookback tests now resolve the same exact-secret context
  used by `awf_read_workspace_log`, preserving the behavior assertions without
  disabling checks or weakening redaction expectations.
- Complete: No avoidable T17 runtime overhead was removed in this iteration;
  focused MCP xdist coverage passed, and the actionable failure was stale
  offset expectations rather than test setup cost.
- Complete: Focused verification passed. Broad AWF/GitHub full coverage was not
  run locally; AWF owns that gate after agent completion.

Additional files changed:

- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_doctor.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_support_bundle.py -q --tb=short -ra
# failed: 3 MCP workspace-log assignment lookback tests expected offset 5903/95903 but actual expanded read offset was 5896/95896
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py -q -k 'visible_assignment_context or assignment_lookback_failure or assignment_lookback_still_mid_fragment' --tb=short -ra
# 3 passed, 38 deselected

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_mcp_cli.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_002.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/runtime/test_log_redaction.py tests/unit/service/test_doctor.py tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/service/test_support_bundle.py -q --tb=short -ra
# 213 passed

uv run --python 3.12 --extra dev pytest tests/unit/mcp -n 8 --dist=loadscope -q --tb=short -ra
# 281 passed

uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py
# All checks passed
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## CI Repair: MCP Log Test Line-Limit Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: GitHub Actions run `26962953418` was inspected after the existing
  MCP offset fix. The current `python-full-coverage` job ran tests to
  completion and failed `test_first_party_code_files_stay_under_line_limit`
  because `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
  had grown to 2,136 lines.
- Complete: `TestWorkspaceLogs` was moved whole into
  `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py` with the
  same fixture/helper behavior, preserving the workspace-log regression tests
  without weakening assertions.
- Complete: The original part 003 file is now 1,034 lines and the new part 005
  file is 1,137 lines, both below the 1,500-line maintainability limit.
- Complete: Focused verification passed. Broad AWF/GitHub full coverage was not
  run locally; AWF owns that gate after agent completion.

Additional files changed:

- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```text
GitHub Actions run 26962953418, job python-full-coverage:
test_first_party_code_files_stay_under_line_limit failed with
{'tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py': 2136}
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q -k line_limit --tb=short -ra
# 1 passed, 8 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q --tb=short -ra
# 41 passed

uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
# All checks passed
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HIJz7` Exact Secret Ordering Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: full-text `redact_secrets()` now computes exact caller-supplied
  secret matches on the original input together with regex spans before
  rendering output.
- Complete: exact configured secrets that contain URL-credential or
  authorization-header regex substrings are replaced as whole secrets.
- Complete: regex-only URL and authorization redaction still preserves useful
  non-secret context such as `https://<redacted>@host` and
  `Authorization: Bearer <redacted>`.
- Complete: existing slice and byte-slice exact-secret regressions still pass.

Additional files changed:

- `src/awf/common/redaction.py`
- `tests/unit/runtime/test_log_redaction.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k masks_exact_secret_before_pattern_substrings
# failed: URL host/path and authorization-header suffix remained after regex-first masking
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q -k exact_secret
# 3 passed, 25 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_log_redaction.py -q
# 28 passed

uv run --python 3.12 --extra dev ruff check src/awf/common/redaction.py tests/unit/runtime/test_log_redaction.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/common/redaction.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review-Level Comment `issue:4620175517` MCP Lookback Exception Guard Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: successful assignment lookback behavior was left unchanged; the
  only production change wraps the secondary lookback `read_log()` call.
- Complete: when the secondary assignment-lookback `read_log()` raises, the
  helper now returns the existing conservative `(result_text,
  projection_offset, True)` fallback.
- Complete: the MCP tool now returns usable redacted data from the primary read
  instead of surfacing the secondary exception.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k assignment_lookback_exception --tb=short -ra
# failed: ToolError wrapping RuntimeError("transient lookback read failure")
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k assignment_lookback_exception --tb=short -ra
# 1 passed, 17 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q --tb=short -ra
# 18 passed

uv run --python 3.12 --extra dev ruff check src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/mcp/metrics_tools.py
# Success: no issues found in 1 source file
```

Broad AWF/GitHub validation, full coverage, OpenAPI drift, and frontend builds
were not run in the agent phase; AWF owns those gates after completion.

## Review Thread `PRRT_kwDOSJAM6s6HJbA2` Inherited Service Env Secret Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: captured service logs now redact bare non-pattern values from
  inherited secret-like environment keys when `service_environ` is omitted.
- Complete: followed service-log streams redact the same inherited exact secret
  values before writing to the operator terminal.
- Complete: explicit `service_environ` and selected Compose env-file
  exact-secret redaction remain covered by the adjacent focused tests.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k inherited_env_secret --tb=short -ra
# failed: both captured and followed service-log tests returned the raw inherited ANTHROPIC_AUTH_TOKEN value
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k inherited_env_secret --tb=short -ra
# 2 passed, 26 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'inherited_env_secret or compose_env_provider_secret' --tb=short -ra
# 4 passed, 24 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

## CI Repair: Durable Log Coverage Reference Anchor

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: the failing focused repro was reproduced before implementation.
  Pytest could not collect
  `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams`.
- Complete: the durable workspace-log coverage anchor now exists again at the
  registry-referenced part 003 node and still asserts indexed stream listing,
  byte-window reads, and EOF reads through the MCP tool surface.
- Complete: the duplicate copy was removed from part 005, leaving the remaining
  workspace-log edge-case tests there.
- Complete: the touched MCP server part files remain below the 1,500-line
  first-party guard: part 003 is 1,118 lines and part 005 is 1,147 lines.
- Complete: focused verification passed. Broad AWF/GitHub full coverage and CI
  validation were not run locally; AWF owns those gates after agent completion.

Additional files changed:

- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
# ERROR: not found: /workspace/tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams
```

Focused verification after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py::TestWorkspaceLogs::test_lists_and_reads_indexed_log_streams tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
# 2 passed in 1.73s

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q --tb=short -ra
# 42 passed in 38.55s

uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
# All checks passed!

uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q -k line_limit
# 1 passed, 8 deselected in 0.51s
```

## Review Thread `PRRT_kwDOSJAM6s6HKi5o` MCP Artifact Custom Secret Key Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: MCP artifact content now redacts bare non-pattern values from
  custom secret-like Compose env-file keys such as `CUSTOM_CLIENT_SECRET`.
- Complete: MCP payload/artifact exact-secret discovery now reuses the
  service-log secret-key predicate, matching MCP workspace log reads and
  service-log redaction.
- Complete: existing known provider env-key artifact redaction still passes in
  the adjacent focused tests.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k custom_compose_env_secret --tb=short -ra
# failed: returned raw "bare-compose-custom-value" in artifact content
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k custom_compose_env_secret --tb=short -ra
# 1 passed, 32 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'compose_env_file_provider_secret or custom_compose_env_secret' --tb=short -ra
# 3 passed, 30 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
# Success: no issues found in 1 source file
```

## Review-Level Comment `issue:4620175517` Service-Log Short Secret Filter Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: `_service_log_secret_values()` now skips secret-like values shorter
  than four characters from the selected Compose env file, inherited process
  environment, and explicit service environment mappings.
- Complete: longer secret-like values from the same sources remain selected for
  service-log exact-secret redaction.
- Complete: the MCP `_mcp_secret_values()` key-filter item is stale in the
  local branch; `src/awf/mcp/server.py` already imports and uses
  `is_secret_env_key()` when selecting provider env exact secrets.
- Complete: the MCP env-file caching item is treated as a deferred performance
  trade-off in this comment cycle; it does not create a direct secret leak and
  the current per-call path keeps env-file changes visible.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k short_secret_values --tb=short -ra
# failed: the helper still selected a short Compose-env secret-like value before filtering
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k short_secret_values --tb=short -ra
# 1 passed, 28 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'short_secret_values or inherited_env_secret or compose_env_provider_secret' --tb=short -ra
# 5 passed, 24 deselected

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

## Review Thread `PRRT_kwDOSJAM6s6HLG5x` MCP Artifact Unicode Secret Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: likely-text UTF-8 MCP artifacts now redact exact non-ASCII
  Compose/env-file secrets before base64 content is returned.
- Complete: binary artifact exact-secret blocking remains covered by the
  adjacent focused Compose-env binary artifact test.
- Complete: existing ASCII text artifact redaction remains covered by the
  adjacent known-provider and custom secret artifact tests.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k unicode_compose_env_secret --tb=short -ra
# failed: returned raw UTF-8 bytes for "p\u00e4ssw\u00f6rd1234" in artifact content
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k unicode_compose_env_secret --tb=short -ra
# 1 passed, 33 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'compose_env_file_provider_secret or custom_compose_env_secret or unicode_compose_env_secret or binary_artifact_containing_compose_env_file_provider_secret' --tb=short -ra
# 4 passed, 30 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
# Success: no issues found in 1 source file
```

## Review-Level Comment `issue:4620175517` Dead Wrapper And Secret-Key Helper Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: `read_log_chunk()` still decodes the byte-preserving
  `read_log_chunk_bytes()` result with UTF-8 replacement semantics.
- Complete: the dead private `_read_log_chunk()` sync wrapper was removed from
  `src/awf/runtime/logs.py`.
- Complete: secret-env-key classification is now exposed as public
  `awf.service.provider_readiness.is_secret_env_key()` next to
  `KNOWN_SECRET_ENV_KEYS`.
- Complete: `src/awf/service/logs.py`, `src/awf/mcp/server.py`, and
  `src/awf/mcp/metrics_tools.py` now use the public helper instead of importing
  `_is_service_secret_env_key()` from `awf.service.logs`.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/runtime/logs.py`
- `src/awf/service/provider_readiness.py`
- `src/awf/service/logs.py`
- `src/awf/mcp/server.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k secret_env_key --tb=short -ra
# failed: provider_readiness had no public is_secret_env_key helper
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k secret_env_key --tb=short -ra
# 1 passed, 39 deselected

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_logs.py -q -k 'read_uses_threaded_bounded_file_read or read_clamps_offsets' --tb=short -ra
# 2 passed, 24 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_003.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k 'short_secret_values or compose_env_provider_secret or custom_compose_env_secret' --tb=short -ra
# 4 passed, 67 deselected

uv run --python 3.12 --extra dev ruff check src/awf/runtime/logs.py src/awf/service/provider_readiness.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/runtime/logs.py src/awf/service/provider_readiness.py src/awf/service/logs.py src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 5 source files
```

## Review Thread `PRRT_kwDOSJAM6s6HLcza` MCP Artifact Overlapping Secret Bytes Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: likely-text MCP artifacts now redact overlapping configured
  exact-secret byte occurrences before returning base64 content.
- Complete: adjacent repeated exact-secret occurrences remain independently
  redacted, preserving the existing redaction expansion and oversize behavior.
- Complete: existing ASCII and non-ASCII exact-secret artifact redaction remains
  covered by adjacent focused checks.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k overlapping_exact_secret_bytes --tb=short -ra
# failed: returned b"<redacted>abc" for artifact content b"abcabcabc"
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k overlapping_exact_secret_bytes --tb=short -ra
# 1 passed, 34 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py -q -k 'overlapping_exact_secret_bytes or redaction_expansion_triggers_oversized or unicode_compose_env_secret or octet_stream_without_null_bytes' --tb=short -ra
# 4 passed, 31 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_004.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py
# Success: no issues found in 1 source file
```

## Review-Level Comment `issue:4620175517` MCP Log Startup Secret Cache Iteration

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: `awf_read_workspace_log` now reuses the service settings and exact
  extra-secret tuple captured during metrics-tool registration instead of
  resolving them per log request.
- Complete: repeated log polls no longer re-resolve the Compose/env-file
  provider environment.
- Complete: startup-time Compose/env-file provider secrets still redact exact
  bare values from workspace log slices, including the selected custom env file.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/mcp/server.py`
- `src/awf/mcp/metrics_tools.py`
- `tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k startup_redaction_secrets --tb=short -ra
# failed: two log reads increased resolve_service_settings() calls from 1 to 3
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k startup_redaction_secrets --tb=short -ra
# 1 passed, 17 deselected

uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py -q -k 'startup_redaction_secrets or compose_env_provider_secret or custom_compose_env_file_provider_secret' --tb=short -ra
# 3 passed, 15 deselected

uv run --python 3.12 --extra dev ruff check src/awf/mcp/server.py src/awf/mcp/metrics_tools.py tests/unit/mcp/test_mcp_server_parts/test_mcp_server_part_005.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/mcp/server.py src/awf/mcp/metrics_tools.py
# Success: no issues found in 2 source files
```

## Inline Review Thread `PRRT_kwDOSJAM6s6HMFlI` Followed Service Logs Blocked Sink Validation

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`

Requirement status:

- Complete: blocked downstream followed-log writes are now detected by a
  stream-write watchdog and handled through the existing downstream-pipe cleanup
  path.
- Complete: the followed Docker logs subprocess is terminated once when a
  downstream sink blocks, so the main `process.wait()` call is not left behind a
  full subprocess pipe.
- Complete: existing followed-log redaction, broken-pipe cleanup, simultaneous
  pipe cleanup, and default follow streaming behavior remain covered by focused
  adjacent tests.
- Complete: focused verification passed. Broad AWF/GitHub validation, full
  coverage, OpenAPI drift, and frontend builds were not run locally; AWF owns
  those gates after agent completion.

Additional files changed:

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_VALIDATION.md`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k blocked_downstream_write --tb=short -ra
# failed: follow process was not terminated after downstream stdout blocked
```

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k blocked_downstream_write --tb=short -ra
# 1 passed, 29 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q -k 'blocked_downstream_write or broken_stdout_pipe or downstream_stdout_error or simultaneous_broken_pipes or default_follow_runner' --tb=short -ra
# 7 passed, 23 deselected

uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q --tb=short -ra
# 30 passed

uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/service/logs.py
# Success: no issues found in 1 source file
```

## Gaps

None found.
