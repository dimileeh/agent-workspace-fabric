# T09 MCP Setup Tools Validation

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

## Requirement Status

- Add MCP tools `awf_get_setup_status`, `awf_start_local_service`,
  `awf_initialize_project_profile`, and
  `awf_get_client_integration_instructions`: Complete.
- Reuse existing setup/start/init/client service functions and CLI helpers:
  Complete. The MCP tools delegate to setup readiness, start bootstrap,
  onboarding preview/writer, and client plan helpers.
- Keep raw credential values out of MCP inputs and responses: Complete.
- Return setup status as safe refs/status metadata only: Complete.
- Make MCP start repeatable and return structured first-run failures: Complete.
- Make MCP project initialization use the same onboarding writer as the CLI:
  Complete.
- Return client instructions without env-file contents or secret values:
  Complete.
- Update MCP reference/parity docs and focused parity tests: Complete.

## Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_mcp_client_parity_docs.py`
- `tests/unit/mcp/test_mcp_parity_matrix_crossref.py`
- `docs/MCP_REFERENCE.md`
- `docs/MCP_CLIENT_PARITY.md`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/mcp/server.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/mcp/server.py
```

Latest results:

- `tests/unit/mcp/test_setup_tools.py`: 8 passed.
- Focused MCP/parity test set: 33 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HH2Ia

### Requirement Status

- Preserve existing structured handling for `SetupCheckError`,
  `SourceCheckoutError`, and `OSError`: Complete.
- Convert client-instruction planning `RuntimeError` failures into a structured
  `CLIENT_CONFIG_CONFLICT` blocked MCP result without exposing raw exception
  text: Complete.
- Convert client-instruction planning `ValueError` failures into the same
  structured blocked result: Complete.
- Add focused regressions covering the newly handled exception types: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_codex_invalid_home_override_is_structured tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_value_error_is_generic -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_oserror_is_generic tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_codex_invalid_home_override_is_structured tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_value_error_is_generic -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two new regressions failed before the implementation change because
  `RuntimeError` from `CODEX_HOME=~nosuchuser` and planner `ValueError`
  escaped through FastMCP as tool errors.
- New targeted regressions after the implementation change: 2 passed.
- Neighboring OSError plus new planning exception regressions: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HHoLm

### Requirement Status

- Preserve omitted `clients` behavior, which still requests all supported
  clients: Complete.
- Preserve unknown-client validation for non-empty client lists: Complete.
- For explicit `clients: []`, return a successful empty client instruction
  payload before resolving source checkout or env-file state: Complete.
- Add a focused regression proving env-file resolution is skipped for explicit
  empty-client requests: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserves_explicit_empty_clients tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_resolves_relative_source_checkout_apply_command tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_unknown_client_is_structured_error -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because explicit
  `clients: []` still resolved `source_checkout` before returning the empty
  payload.
- Regression test after the implementation change: 1 passed.
- Neighboring client-instruction sanity tests after the implementation change:
  4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HHUuk

### Requirement Status

- Preserve existing setup-status command and next-step output when
  `source_checkout` is not supplied: Complete.
- For explicit `source_checkout` status calls, render a setup command
  containing the resolved checkout path: Complete.
- For successful explicit-checkout status calls, render next-step guidance that
  starts local service with the same resolved checkout path: Complete.
- For blocked explicit-checkout status calls, render next-step guidance that
  re-runs setup dry-run with the same resolved checkout path: Complete.
- Add focused regressions proving the returned guidance preserves the resolved
  explicit checkout path: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two focused regressions failed before implementation because the returned
  `command` was still `awf setup` for explicit-checkout setup-status calls.
- Targeted regressions after the implementation change: 2 passed.
- Focused setup-tools test file: 34 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Preview Failure Logging

### Requirement Status

- Preserve the existing `PROJECT_INIT_FAILED` MCP response and redaction
  behavior: Complete.
- Record the caught preview/probe exception with exception context before
  returning the sanitized result: Complete.
- Include safe operational context in the log entry: project path and template:
  Complete.
- Add a focused regression proving the preview/probe failure path emits an
  exception log while keeping raw exception text out of the MCP response:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression failed as expected because the preview/probe
  failure path returned a sanitized MCP error without emitting any
  `awf.mcp.setup_tools` exception log record.
- Preview/probe logging regression after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue_4620143523

### Requirement Status

- Preserve explicit `SetupCheckError` and `HostSetupConfigError` structured
  reason-code payloads: Complete.
- Convert generic setup-status probe failures into a redacted first-run blocked
  result instead of allowing the raw exception to escape: Complete.
- Expose only the exception type for generic probe failures: Complete.
- Add a regression for an `_run_setup` `OSError` containing a token-like value
  and path: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change with a FastMCP
  `ToolError` that included the raw socket path and token-like value from the
  raised `OSError`.
- Regression test after the implementation change: 1 passed.
- Focused neighboring setup-status tests: 4 passed.
- Focused ruff: passed after import-order fix.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HDuez

### Requirement Status

- Preserve normal resolved-path behavior for valid absolute and relative paths:
  Complete.
- Treat `ValueError` from user path expansion/resolution like the existing
  guarded normalization failures: Complete.
- Return structured MCP errors for malformed `project_path` values passed to
  `awf_initialize_project_profile`: Complete.
- Preserve structured MCP behavior for malformed `source_checkout` values passed
  to setup/start tools: Complete.
- Keep response payloads secret-free and avoid broad validation in the agent
  phase: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_value_error_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_value_error_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_value_error_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_value_error_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_expanduser_failure_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_resolve_failure_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_value_error_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_expanduser_failure_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_value_error_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because
  `Path.resolve()` raised `ValueError` for embedded-NUL paths and FastMCP wrapped
  it as `ToolError`.
- Regression tests after the implementation change: 3 passed.
- Related path-normalization MCP tests: 7 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HDeYw Start Input Resolution Errors

### Requirement Status

- Preserve existing structured handling for `SourceCheckoutError` and
  `ServiceBootstrapError`: Complete.
- Convert setup/config/path failures from start input resolution into
  redacted MCP error responses: Complete.
- Do not surface raw exception text or path-like details from those failures:
  Complete.
- Add a focused regression for start input resolution failure: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_reports_structured_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_reuses_bootstrap_and_is_idempotent tests/unit/mcp/test_setup_tools.py::test_start_local_service_offloads_sync_preparation -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression failed as expected because the raw
  `ValueError` escaped through FastMCP `ToolError`.
- Targeted regression after the implementation change: 1 passed.
- Adjacent start-tool checks after splitting input resolution from bootstrap:
  4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HDb5N

### Requirement Status

- Preserve existing project-path existence and directory validation behavior:
  Complete.
- Convert `expanduser()` and `resolve()` failures during project-path
  resolution into structured project-init responses: Complete.
- Do not surface raw path-resolution exception text in MCP response content:
  Complete.
- Add focused regressions for guarded project-init path resolution: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_expanduser_failure_returns_structured_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_expanduser_failure_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_resolve_failure_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_expanduser_failure_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_expanduser_failure_uses_guarded_fallback -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression failed as expected because
  `Path.expanduser()` raised through FastMCP `ToolError`.
- Project-init and adjacent guarded source-checkout regressions after the
  implementation change: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Profile Probe And Marker Count Contract

### Requirement Status

- Preserve project path existence and directory validation behavior: Complete.
- Convert existing-profile probe filesystem failures into structured
  `PROJECT_INIT_FAILED` MCP errors without surfacing raw exception text:
  Complete.
- Preserve existing onboarding preview and write-profile behavior: Complete.
- Preserve persisted-config integer `source_checkout.marker_count` behavior:
  Complete.
- Preserve probed explicit-checkout `source_checkout.marker_count=null`
  behavior: Complete.
- Document the nullable marker-count response contract in the MCP parity docs:
  Complete.
- Add focused regressions for the probe guard and marker-count documentation:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_mcp_client_parity_docs.py`
- `docs/MCP_CLIENT_PARITY.md`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_is_structured tests/unit/mcp/test_mcp_client_parity_docs.py::test_first_run_setup_tools_are_documented_as_local_secret_free_mcp_surface -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_uses_onboarding_writer tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regressions failed as expected because the
  existing-profile probe exception escaped through FastMCP `ToolError`, and the
  parity docs did not publish the nullable marker-count contract.
- Targeted regressions after the implementation/doc changes: 2 passed.
- Adjacent project-init and source-checkout status checks: 6 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Expanduser Fallback

### Requirement Status

- Preserve existing setup-status and start-service behavior for normal explicit
  `source_checkout` values: Complete.
- When `expanduser()` raises during setup-status path resolution, fall back to
  the guarded absolute path behavior instead of escaping from the MCP tool:
  Complete.
- When `expanduser()` raises during start-service path resolution, fall back to
  the guarded absolute path behavior instead of escaping from the MCP tool:
  Complete.
- Add focused regressions for both MCP tools: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_expanduser_failure_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_expanduser_failure_uses_guarded_fallback -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regressions failed as expected because both tools escaped
  through FastMCP `ToolError` from the raw `RuntimeError` raised by
  `Path.expanduser()`.
- Regression tests after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: Implemented Parity Coverage Reference

### Requirement Status

- Keep `Local first-run setup/start/init/client` marked `MCP implemented`:
  Complete.
- Add explicit executable coverage references for the local first-run MCP row:
  Complete.
- Use existing focused MCP setup-tool tests that cover setup, start, init, and
  client instruction behavior: Complete.
- Run the focused failing repro and the referenced setup-tool tests: Complete.
- Leave broad AWF/GitHub validation and full coverage gates to AWF after the
  agent phase: Complete.

### Evidence

Files changed:

- `tests/unit/contracts/test_registry_smoke.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_tools_are_registered tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_start_local_service_offloads_sync_preparation tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_uses_onboarding_writer tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev ruff check tests/unit/contracts/test_registry_smoke.py
```

Latest results:

- Focused CI repro failed before the repair with missing coverage reference:
  `['Local first-run setup/start/init/client']`.
- Focused CI repro after the repair: 1 passed.
- Referenced setup-tool tests: 5 passed.
- Focused ruff: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HB0-m

### Requirement Status

- Preserve omitted `clients` behavior: default to every supported client:
  Complete.
- Preserve explicit non-empty `clients` behavior: Complete.
- Treat explicit `clients: []` as zero requested client plans: Complete.
- Add a focused regression for the explicit empty-client request: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because an explicit
  empty `clients` array returned client plans for every supported client.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 ValueError Preview Redaction

### Requirement Status

- Preserve explicit project-path existence and directory errors as
  `PROJECT_INIT_INVALID_PATH`: Complete.
- Treat `ValueError` from onboarding preview like other preview-construction
  failures: Complete.
- Do not include raw `ValueError` text in MCP response content: Complete.
- Return the fixed preview-failure message and `PROJECT_INIT_FAILED` code with
  safe project/template context: Complete.
- Add a focused regression for `ValueError` redaction: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression failed as expected because the MCP response
  returned `PROJECT_INIT_INVALID_PATH` from a preview `ValueError`.
- Preview-failure redaction checks after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Source Checkout Config Status

### Requirement Status

- Preserve normal host-config error responses when `source_checkout` is not
  provided: Complete.
- When `source_checkout` is provided and host config is valid, include provider
  status, client status, and consent metadata from disk in the response:
  Complete.
- When `source_checkout` is provided and host config is corrupt or unreadable,
  fall back to an empty `HostSetupConfig()` without failing the explicit
  checkout probe: Complete.
- Preserve probed explicit-checkout `source_checkout` metadata from setup
  readiness details: Complete.
- Add focused regressions for the valid-config and corrupt-config
  explicit-checkout branches: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_hides_stale_persisted_source_checkout_when_revalidation_blocks tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regressions failed as expected because the explicit
  source-checkout path never called `read_host_setup_config()`.
- This repair supersedes the earlier MCP wrapper skip-read behavior while
  preserving the corrupt-config fallback for explicit checkout probes.
- Source-checkout valid-config and corrupt-config regressions after the
  implementation change: 2 passed.
- Adjacent setup-status checks after adding the no-source-checkout preservation
  regression: 5 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HBQTg

### Requirement Status

- Preserve persisted source-checkout metadata when the current readiness probe
  succeeds: Complete.
- Preserve explicit-checkout probed metadata behavior: Complete.
- When rendered readiness includes a blocking source-checkout issue, do not
  report the persisted checkout as present: Complete.
- Add a focused regression proving stale persisted checkout metadata is hidden
  when source-checkout revalidation blocks: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_hides_stale_persisted_source_checkout_when_revalidation_blocks -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read tests/unit/mcp/test_setup_tools.py::test_get_setup_status_hides_stale_persisted_source_checkout_when_revalidation_blocks -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `source_checkout` still reported the stale persisted root as present.
- Regression test after the implementation change: 1 passed.
- Adjacent setup-status preservation checks: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Marker Count Schema

### Requirement Status

- Preserve persisted-config `source_checkout.marker_count` behavior: Complete.
- Add `marker_count` to the probed explicit-checkout status payload: Complete.
- Add a focused regression expectation for the explicit-checkout payload shape:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression: failed as expected because the probed
  explicit-checkout payload omitted `marker_count`.
- Post-implementation regression: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HA7jn

### Requirement Status

- Preserve successful client instruction behavior and conflict-plan behavior:
  Complete.
- Keep unknown-client and source-checkout structured errors unchanged:
  Complete.
- Convert `SetupCheckError` raised during client config planning into the
  existing reason-coded first-run MCP error payload: Complete.
- Convert unexpected `OSError` raised during client config planning into a
  generic structured client-config blocker without raw exception text:
  Complete.
- Add focused regressions proving planner failures return through
  `safe_result` without leaking raw exception detail: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_setup_error_is_structured tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_oserror_is_generic -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because
  `build_client_config_plan` exceptions escaped through FastMCP `ToolError`.
- Regression tests after the implementation change: 2 passed.
- Focused setup-tools test file: 18 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523

### Requirement Status

- Preserve `awf_start_local_service` option validation and response payload
  behavior: Complete.
- Offload `_resolve_start_source_checkout` and `_resolve_start_bootstrap_inputs`
  from the event-loop thread with `asyncio.to_thread`: Complete.
- Keep `SourceCheckoutError` and `ServiceBootstrapError` structured error
  handling unchanged: Complete.
- Add a focused regression proving start-service preparation helpers run away
  from the event-loop thread: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_offloads_sync_preparation -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the start
  preparation helpers ran on the event-loop thread.
- Regression test after the implementation change: 1 passed.
- Focused setup-tools test file: 16 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HAciC

### Requirement Status

- Preserve existing client instruction commands when `source_checkout` is not
  provided: Complete.
- Preserve existing absolute explicit-checkout command rendering: Complete.
- Resolve a relative explicit `source_checkout` before rendering each
  per-client `apply_command`: Complete.
- Include the same resolved explicit-checkout command in the top-level next
  steps: Complete.
- Keep client instructions secret-free and otherwise schema-compatible:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_resolves_relative_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `_resolve_client_env_file` received `source checkout` and the rendered
  command kept that relative path instead of the checkout resolved from the MCP
  server cwd.
- Regression test after the implementation change: 1 passed.
- Focused setup-tools test file: 15 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HAbmv

### Requirement Status

- Preserve persisted-config `source_checkout` status behavior when
  `source_checkout` is not provided: Complete.
- Preserve explicit `source_checkout` status behavior that skips
  `read_host_setup_config()` after `_run_setup`: Complete.
- Surface safe probed checkout metadata from rendered readiness details on the
  explicit-checkout path: Complete.
- Add a focused regression proving explicit-checkout setup status reports the
  probed checkout as present: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Pre-implementation regression: failed as expected because
  `payload["source_checkout"]` was `{"present": false}` despite
  `details.source_checkout` containing the probed checkout.
- Focused setup-status regressions: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HAAxL

### Requirement Status

- Keep known `ValueError` onboarding validation errors unchanged: Complete.
- Keep unexpected onboarding preview failures as structured MCP errors with
  `PROJECT_INIT_FAILED`: Complete.
- Do not include unexpected exception text in the MCP response message:
  Complete.
- Preserve useful non-secret context in `detail` for the project path and
  template: Complete.
- Add a regression proving unexpected preview exception text is not surfaced:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["message"]` included `/srv/awf/internal/config.yml traceback frame`
  from the raised `RuntimeError`.
- Regression test after the implementation change: 1 passed.
- Focused setup-tools test file: 14 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HAAvH

### Requirement Status

- Keep the existing async MCP tool signatures and response payloads stable:
  Complete.
- Offload `_get_setup_status_result` from `awf_get_setup_status` with
  `asyncio.to_thread`: Complete.
- Offload `_initialize_project_profile_result` from
  `awf_initialize_project_profile` with `asyncio.to_thread`: Complete.
- Offload `_client_integration_instructions_result` from
  `awf_get_client_integration_instructions` with `asyncio.to_thread`:
  Complete.
- Add a focused regression proving the blocking setup helper work does not run
  on the event-loop thread: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_status_init_and_client_tools_offload_blocking_work -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because all three
  helper dependencies recorded the event-loop thread.
- Regression test after the implementation change: 1 passed.
- Focused setup-tools test file: 13 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6G_-yQ

### Requirement Status

- Preserve existing client instruction commands when `source_checkout` is not
  provided: Complete.
- Include `--source-checkout <path>` in each per-client `apply_command` when an
  explicit checkout is used to build the plan: Complete.
- Include the same explicit-checkout command in the top-level next steps:
  Complete.
- Keep client instructions secret-free and otherwise schema-compatible:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `apply_command` remained `awf setup --client claude` without the explicit
  `--source-checkout` argument.
- Regression test after the implementation change: 1 passed.
- Focused setup-tools test file: 12 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6G_-HM

### Requirement Status

- Preserve successful and warning setup status responses as non-error MCP tool
  calls: Complete.
- Mark `awf_get_setup_status` responses as MCP errors when rendered readiness
  status is `blocked` or `failed`: Complete.
- Keep the existing setup status response payload shape and redaction behavior:
  Complete.
- Add a focused regression proving blocked and failed readiness payloads set
  `result.isError`: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change with `result.isError`
  still `False` for both `blocked` and `failed` setup readiness payloads.
- Regression test after the implementation change: 2 passed.
- Focused setup-tools test file: 11 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6G_-HK

### Requirement Status

- Preserve normal `awf_get_setup_status` behavior when `source_checkout` is not
  provided, including safe persisted provider/client/config metadata: Complete.
- For explicit `source_checkout`, do not call `read_host_setup_config()` after
  `_run_setup`: Complete.
- Keep the MCP response schema stable and secret-free on the explicit-checkout
  path: Complete.
- Add a regression proving a corrupt host config cannot turn an explicit
  checkout status probe into an MCP error: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test: 1 passed. Before the implementation change, this test failed
  because `awf_get_setup_status` returned an MCP error from
  `HostSetupConfigError` after `_run_setup`.
- Focused setup-tools test file: 9 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.
