# T09 MCP Setup Tools Plan

## Problem Statement And Scope

Implement T09 from `TODO/awf-full-installer-first-run-setup-backlog.md` by
exposing first-run setup/start/init/client capabilities through AWF's local MCP
server. The implementation contract is the AWF-supplied saved plan at
`docs/awf-plans/ws_c9cb06c77fbb47d38f3d774a.md`.

## Requirements Checklist

- Add MCP tools `awf_get_setup_status`, `awf_start_local_service`,
  `awf_initialize_project_profile`, and
  `awf_get_client_integration_instructions`.
- Reuse existing setup/start/init/client service functions and CLI helpers.
- Keep raw credential values out of MCP inputs and responses.
- Return setup status as safe refs/status metadata only.
- Make MCP start repeatable and return structured first-run failures.
- Make MCP project initialization use the same onboarding writer as the CLI.
- Return client instructions without env-file contents or secret values.
- Update MCP reference/parity docs and focused parity tests.

## Implementation Steps

1. Add focused failing MCP setup tool tests.
2. Add `src/awf/mcp/setup_tools.py` with bounded tool schemas and pure payload
   helpers.
3. Register setup tools from `src/awf/mcp/server.py`.
4. Update MCP reference/parity docs and any guarded docs tests.
5. Run focused MCP tests, focused parity tests, focused ruff, and focused mypy.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/mcp/server.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py tests/unit/mcp/test_mcp_parity_matrix_crossref.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/mcp/server.py
```

Full AWF/GitHub validation and coverage gates are intentionally left to AWF
after the agent phase.

## Review Repair: issue:4620143523 Expanduser Fallback

### Problem Statement And Scope

The review reports that `awf_get_setup_status` and
`awf_start_local_service` resolve explicit `source_checkout` values with direct
`Path(...).expanduser()` calls, while the client-instruction tool already uses
a guarded resolver that catches `OSError` and `RuntimeError` from path
expansion/resolution.

Scope is limited to making all three MCP setup tools use the same defensive
source-checkout path resolver. The existing helper behavior, response schemas,
and first-run error handling stay unchanged.

### Requirements Checklist

- Preserve existing setup-status and start-service behavior for normal explicit
  `source_checkout` values.
- When `expanduser()` raises during setup-status path resolution, fall back to
  the guarded absolute path behavior instead of escaping from the MCP tool.
- When `expanduser()` raises during start-service path resolution, fall back to
  the guarded absolute path behavior instead of escaping from the MCP tool.
- Add focused regressions for both MCP tools.

### Implementation Steps

1. Add focused failing MCP regressions that force `Path.expanduser()` to raise
   during setup-status and start-service source-checkout resolution.
2. Replace the direct setup-status and start-service `expanduser()` call sites
   with the existing guarded source-checkout resolver.
3. Run the targeted regressions and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_expanduser_failure_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_expanduser_failure_uses_guarded_fallback -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HDeYw Start Input Resolution Errors

### Problem Statement And Scope

The review reports that `awf_start_local_service` catches
`SourceCheckoutError` and `ServiceBootstrapError`, but not setup/config/path
errors raised while resolving start bootstrap inputs before service bootstrap
begins. Those exceptions can escape the MCP tool through FastMCP instead of
being routed through redacted `safe_result` output.

Scope is limited to guarding MCP start input resolution failures. Bootstrap
failure classification and successful start behavior stay unchanged.

### Requirements Checklist

- Preserve existing structured handling for `SourceCheckoutError` and
  `ServiceBootstrapError`.
- Convert setup/config/path failures from start input resolution into
  redacted MCP error responses.
- Do not surface raw exception text or path-like details from those failures.
- Add a focused regression for start input resolution failure.

### Implementation Steps

1. Add a focused failing MCP regression where start bootstrap input resolution
   raises a path-like `ValueError`.
2. Catch setup/config/path exceptions around the threaded input-resolution
   step and return a generic structured start error through `safe_result`.
3. Run the targeted regression and focused checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HDb5N

### Problem Statement And Scope

The PR review reports that `awf_initialize_project_profile` resolves
`project_path` with a direct `Path(...).expanduser().resolve()` before the
structured project-init validation path. If user expansion or path resolution
raises, the MCP tool can surface an unhandled tool error instead of a structured
project-init response.

Scope is limited to guarding MCP project-init path resolution with the same
fallback behavior used for explicit setup-tool checkout paths.

### Requirements Checklist

- Preserve existing project-path existence and directory validation behavior.
- Convert `expanduser()` and `resolve()` failures during project-path
  resolution into structured project-init responses.
- Do not surface raw path-resolution exception text in MCP response content.
- Add focused regressions for guarded project-init path resolution.

### Implementation Steps

1. Add focused failing MCP regressions where `project_path` expansion or
   resolution raises.
2. Route project-init path resolution through a guarded path resolver.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_expanduser_failure_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_resolve_failure_returns_structured_error -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Profile Probe And Marker Count Contract

### Problem Statement And Scope

The review reports two remaining setup-tool contract gaps:
`_initialize_project_profile_result` probes for an existing project profile
before the guarded onboarding preview block, and the setup-status
`source_checkout.marker_count` response contract is implicit when probed
checkout metadata cannot provide a marker count.

Scope is limited to guarding the existing-profile filesystem probe through the
same structured MCP error path as onboarding preview failures and documenting
the existing nullable `source_checkout.marker_count` contract. Existing
persisted integer and probed null payload behavior stays unchanged.

### Requirements Checklist

- Preserve project path existence and directory validation behavior.
- Convert existing-profile probe filesystem failures into structured
  `PROJECT_INIT_FAILED` MCP errors without surfacing raw exception text.
- Preserve existing onboarding preview and write-profile behavior.
- Preserve persisted-config integer `source_checkout.marker_count` behavior.
- Preserve probed explicit-checkout `source_checkout.marker_count=null`
  behavior.
- Document the nullable marker-count response contract in the MCP parity docs.
- Add focused regressions for the probe guard and marker-count documentation.

### Implementation Steps

1. Add a focused failing MCP regression for an existing-profile probe
   `OSError` with path-like exception text.
2. Add a focused docs assertion for the nullable marker-count response
   contract.
3. Move `_existing_project_profile_path(repository)` inside the existing
   onboarding preview `try` block.
4. Update the MCP parity docs to state that setup-status
   `source_checkout.marker_count` is `integer | null`.
5. Run the targeted regressions and focused checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_is_structured tests/unit/mcp/test_mcp_client_parity_docs.py::test_first_run_setup_tools_are_documented_as_local_secret_free_mcp_surface -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_mcp_client_parity_docs.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Marker Count Schema

### Problem Statement And Scope

The review reports that `_setup_status_source_checkout` returns
`marker_count` when source-checkout metadata comes from persisted host config,
but omits that key when the same top-level status is populated from the
explicit-checkout readiness probe.

Scope is limited to keeping the `source_checkout` response shape stable for the
probed explicit-checkout path.

### Requirements Checklist

- Preserve persisted-config `source_checkout.marker_count` behavior.
- Add `marker_count` to the probed explicit-checkout status payload.
- Add a focused regression expectation for the explicit-checkout payload shape.

### Implementation Steps

1. Update the explicit-checkout setup-status regression to expect
   `marker_count`.
2. Add the stable `marker_count` field to the probed fallback payload.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: Implemented Parity Coverage Reference

### Problem Statement And Scope

GitHub CI fails
`tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference`
because the MCP parity matrix now marks `Local first-run setup/start/init/client`
as `MCP implemented`, but the implemented-parity coverage reference map does
not point that row at executable MCP contract coverage.

Scope is limited to adding the missing traceability entry for the already
implemented first-run setup/start/init/client MCP tools. The parity row,
tool behavior, and quality gate stay unchanged.

### Requirements Checklist

- Keep `Local first-run setup/start/init/client` marked `MCP implemented`.
- Add explicit executable coverage references for the local first-run MCP row.
- Use existing focused MCP setup-tool tests that cover setup, start, init, and
  client instruction behavior.
- Run the focused failing repro and the referenced setup-tool tests.
- Leave broad AWF/GitHub validation and full coverage gates to AWF after the
  agent phase.

### Implementation Steps

1. Confirm the focused CI repro fails with the missing coverage-reference
   assertion.
2. Add the missing implemented-parity coverage map entry pointing at existing
   `tests/unit/mcp/test_setup_tools.py` node IDs.
3. Re-run the focused CI repro and the referenced setup-tool tests.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_tools_are_registered tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_start_local_service_offloads_sync_preparation tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_uses_onboarding_writer tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HB0-m

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` treats an
explicit empty `clients` array the same as omitting `clients`, because the MCP
wrapper passes `clients or list(CLIENT_DESCRIPTORS)` into the helper. That
causes a request for no client plans to expand to every supported MCP client.

Scope is limited to preserving the difference between omitted `clients` and an
explicit empty list at the MCP tool boundary.

### Requirements Checklist

- Preserve omitted `clients` behavior: default to every supported client.
- Preserve explicit non-empty `clients` behavior.
- Treat explicit `clients: []` as zero requested client plans.
- Add a focused regression for the explicit empty-client request.

### Implementation Steps

1. Add the focused failing MCP regression for `clients: []`.
2. Change the MCP wrapper defaulting expression to check `clients is None`.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 ValueError Preview Redaction

### Problem Statement And Scope

The review reports that `_initialize_project_profile_result` returns
`str(exc)` from `ValueError` raised by `preview_project_onboarding`, while the
generic preview-failure path deliberately suppresses raw exception text. A
`ValueError` can include path-like or internal validation context, and
`PROJECT_INIT_INVALID_PATH` does not describe template/preview validation
failures after the project path has already passed the MCP path checks.

Scope is limited to MCP project-initialization preview error handling and its
focused regression.

### Requirements Checklist

- Preserve explicit project-path existence and directory errors as
  `PROJECT_INIT_INVALID_PATH`.
- Treat `ValueError` from onboarding preview like other preview-construction
  failures.
- Do not include raw `ValueError` text in MCP response content.
- Return the fixed preview-failure message and `PROJECT_INIT_FAILED` code with
  safe project/template context.
- Add a focused regression for `ValueError` redaction.

### Implementation Steps

1. Add a focused failing MCP regression where `preview_project_onboarding`
   raises a path-like `ValueError`.
2. Remove the special `ValueError` message passthrough so preview
   `ValueError`s use the generic preview-failure response.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Source Checkout Config Status

### Problem Statement And Scope

The review reports that `_get_setup_status_result` always substitutes an empty
`HostSetupConfig()` when `source_checkout` is provided. That keeps corrupt host
config from aborting an explicit-checkout dry-run probe, but it also drops valid
provider status, client status, and consent metadata from the MCP response.

Scope is limited to the MCP setup-status wrapper. `_run_setup` must keep its
explicit-checkout dry-run behavior, and corrupt host config must still fall back
gracefully for explicit checkout probes.
This supersedes the earlier MCP wrapper skip-read implementation while
preserving its corrupt-config safety goal.

### Requirements Checklist

- Preserve normal host-config error responses when `source_checkout` is not
  provided.
- When `source_checkout` is provided and host config is valid, include provider
  status, client status, and consent metadata from disk in the response.
- When `source_checkout` is provided and host config is corrupt or unreadable,
  fall back to an empty `HostSetupConfig()` without failing the explicit
  checkout probe.
- Preserve probed explicit-checkout `source_checkout` metadata from setup
  readiness details.
- Add focused regressions for the valid-config and corrupt-config
  explicit-checkout branches.

### Implementation Steps

1. Add/update focused failing MCP setup-status regressions for explicit
   `source_checkout` with valid host config and corrupt host config.
2. Change `_get_setup_status_result` to attempt `read_host_setup_config()` and
   catch `HostSetupConfigError` as an explicit-checkout-only fallback.
3. Run targeted regressions and focused setup-tools checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HBQTg

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` can show stale persisted
source-checkout metadata as `source_checkout.present=true` even when the
current setup readiness probe blocks on source-checkout revalidation. That
contradicts the current probe result and can mislead operators about whether
the checkout is usable.

Scope is limited to the MCP setup-status source-checkout presentation and its
focused regression.

### Requirements Checklist

- Preserve persisted source-checkout metadata when the current readiness probe
  succeeds.
- Preserve explicit-checkout probed metadata behavior.
- When rendered readiness includes a blocking source-checkout issue, do not
  report the persisted checkout as present.
- Add a focused regression proving stale persisted checkout metadata is hidden
  when source-checkout revalidation blocks.

### Implementation Steps

1. Add a focused failing MCP regression for blocked source-checkout readiness
   with persisted metadata.
2. Teach the setup-status source-checkout helper to let a current blocking
   source-checkout issue override persisted metadata.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_hides_stale_persisted_source_checkout_when_revalidation_blocks -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HAbmv

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` drops explicit
`source_checkout` probe metadata from the setup readiness payload. The CLI
dry-run JSON includes the probed checkout under `details.source_checkout`, but
the MCP wrapper currently builds top-level `source_checkout` only from persisted
host config. On explicit-checkout dry-runs the wrapper intentionally uses an
empty in-memory config, so the response incorrectly reports
`source_checkout.present=false`.

Scope is limited to MCP setup status parity for explicit source-checkout
metadata and its focused regression test.

### Requirements Checklist

- Preserve persisted-config `source_checkout` status behavior when
  `source_checkout` is not provided.
- Preserve explicit `source_checkout` status behavior that skips
  `read_host_setup_config()` after `_run_setup`.
- Surface safe probed checkout metadata from rendered readiness details on the
  explicit-checkout path.
- Add a focused regression proving explicit-checkout setup status reports the
  probed checkout as present.

### Implementation Steps

1. Update the explicit source-checkout MCP regression to expect top-level
   probed checkout metadata.
2. Add a small setup-status helper that falls back from persisted config
   metadata to rendered readiness `details.source_checkout`.
3. Use the helper in `_get_setup_status_result`.
4. Run the targeted regression and a focused lint check for the changed files.

### Assumptions/Changes

- Also assert the existing no-explicit-checkout path still returns persisted
  source-checkout metadata, because the fallback helper intentionally preserves
  persisted config metadata before consulting rendered readiness details.
- Add a focused type check for the changed production module.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates are intentionally left to AWF
after the agent phase.

## Review Repair: PRRT_kwDOSJAM6s6G_-HK

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` diverges from
`awf setup --dry-run --source-checkout` by reading host setup config after
`_run_setup` even when an explicit `source_checkout` was provided. The CLI
intentionally skips that read on explicit-checkout dry-runs so a corrupt or
secret-bearing `~/.awf/config.yml` cannot abort a read-only checkout probe.

Scope is limited to MCP setup status parity and its focused regression test.

### Requirements Checklist

- Preserve normal `awf_get_setup_status` behavior when `source_checkout` is not
  provided, including safe persisted provider/client/config metadata.
- For explicit `source_checkout`, do not call `read_host_setup_config()` after
  `_run_setup`.
- Keep the MCP response schema stable and secret-free on the explicit-checkout
  path.
- Add a regression proving a corrupt host config cannot turn an explicit
  checkout status probe into an MCP error.

### Implementation Steps

1. Add the focused failing MCP regression for explicit source-checkout status.
2. Change `_get_setup_status_result` to use an empty in-memory
   `HostSetupConfig` when `source_checkout` is explicit, matching the CLI's
   dry-run config-read skip while preserving response keys.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_skips_host_config_read -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HA7jn

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` catches
`SetupCheckError` and `SourceCheckoutError` while normalizing clients and
resolving the env file, but builds each `ClientConfigPlan` after that guarded
block. If read-only client planning raises a reason-coded setup failure or an
unexpected filesystem error, the exception can escape through `asyncio.to_thread`
instead of being returned as a safe structured MCP result.

Scope is limited to wrapping client-instruction plan construction in the same
safe error boundary and adding a focused regression for a planning failure.

### Requirements Checklist

- Preserve successful client instruction behavior and conflict-plan behavior.
- Keep unknown-client and source-checkout structured errors unchanged.
- Convert `SetupCheckError` raised during client config planning into the
  existing reason-coded first-run MCP error payload.
- Convert unexpected `OSError` raised during client config planning into a
  generic structured client-config blocker without raw exception text.
- Add a focused regression proving a planner `SetupCheckError` returns through
  `safe_result` without leaking exception detail.

### Implementation Steps

1. Add focused failing MCP regressions for post-normalization client planning
   `SetupCheckError` and `OSError` failures.
2. Move client plan construction inside the guarded block and add a generic
   `OSError` handler for read-only client config planning failures.
3. Run the targeted regression and focused setup-tools checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_setup_error_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_oserror_is_generic -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523

### Problem Statement And Scope

The review reports that `_start_local_service_result` still runs synchronous
start-preparation helpers on the MCP event-loop thread. Those helpers validate
source checkout metadata and resolve bootstrap inputs with filesystem stats
before `run_service_bootstrap` is awaited.

Scope is limited to offloading the synchronous preparation block for
`awf_start_local_service`; the existing async bootstrap delegation and response
payloads must remain unchanged.

### Requirements Checklist

- Preserve `awf_start_local_service` option validation and response payload
  behavior.
- Offload `_resolve_start_source_checkout` and `_resolve_start_bootstrap_inputs`
  from the event-loop thread with `asyncio.to_thread`.
- Keep `SourceCheckoutError` and `ServiceBootstrapError` structured error
  handling unchanged.
- Add a focused regression proving start-service preparation helpers run away
  from the event-loop thread.

### Implementation Steps

1. Add the focused failing MCP regression for start-service preparation
   offloading.
2. Wrap source-checkout and bootstrap-input preparation in a synchronous helper
   and call it via `asyncio.to_thread`.
3. Run the targeted regression and focused setup-tools checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_offloads_sync_preparation -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HAAxL

### Problem Statement And Scope

The PR review reports that `awf_initialize_project_profile` catches a broad
preview failure and interpolates `str(exc)` into the MCP error message. That
can disclose internal paths or state from unexpected exception text before the
response reaches the normal redaction boundary.

Scope is limited to the onboarding preview failure message returned by the MCP
setup tool and its focused regression test.

### Requirements Checklist

- Keep known `ValueError` onboarding validation errors unchanged.
- Keep unexpected onboarding preview failures as structured MCP errors with
  `PROJECT_INIT_FAILED`.
- Do not include unexpected exception text in the MCP response message.
- Preserve useful non-secret context in `detail` for the project path and
  template.
- Add a regression proving unexpected preview exception text is not surfaced.

### Implementation Steps

1. Add the focused failing MCP regression for unexpected onboarding preview
   failures.
2. Change the unexpected preview failure message to a generic string that does
   not interpolate the caught exception.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HAAvH

### Problem Statement And Scope

The PR review reports that three async MCP setup handlers call synchronous
helpers directly: `awf_get_setup_status`,
`awf_initialize_project_profile`, and
`awf_get_client_integration_instructions`. Those helpers perform filesystem,
host config, system-check, and client-plan work, so running them directly can
block the MCP server event loop.

Scope is limited to preserving the existing async MCP public surface while
offloading the three synchronous helper calls to worker threads.

### Requirements Checklist

- Keep the existing async MCP tool signatures and response payloads stable.
- Offload `_get_setup_status_result` from `awf_get_setup_status` with
  `asyncio.to_thread`.
- Offload `_initialize_project_profile_result` from
  `awf_initialize_project_profile` with `asyncio.to_thread`.
- Offload `_client_integration_instructions_result` from
  `awf_get_client_integration_instructions` with `asyncio.to_thread`.
- Add a focused regression proving the blocking setup helper work does not run
  on the event-loop thread.

### Implementation Steps

1. Add a focused failing MCP regression that records the event-loop thread and
   verifies the blocking helper dependencies run on worker threads for the
   three affected tools.
2. Add `asyncio.to_thread` offloading in the three async wrappers.
3. Run the targeted regression and focused setup-tools test file, plus focused
   lint/type checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_status_init_and_client_tools_offload_blocking_work -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6G_-yQ

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` builds
the client plan against an explicit `source_checkout` env file, but returns an
apply command and next step that omit `--source-checkout`. Running the returned
command can then resolve a persisted/default env file instead of the checkout
used for the MCP instructions.

Scope is limited to preserving the explicit checkout path in MCP client
instruction commands and adding a focused regression.

### Requirements Checklist

- Preserve existing client instruction commands when `source_checkout` is not
  provided.
- Include `--source-checkout <path>` in each per-client `apply_command` when an
  explicit checkout is used to build the plan.
- Include the same explicit-checkout command in the top-level next steps.
- Keep client instructions secret-free and otherwise schema-compatible.

### Implementation Steps

1. Add the focused failing MCP regression for explicit source-checkout client
   instructions.
2. Thread the explicit checkout path into client instruction payload and
   next-step rendering.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HAciC

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` validates
an explicit relative `source_checkout` against the MCP server's current working
directory, but renders `apply_command` and `next_steps` with the unresolved
relative path. An operator copying the returned command from a different shell
directory can then apply or validate a different checkout than the one used for
the displayed plan.

Scope is limited to resolving an explicit checkout path before it is used for
MCP client instruction planning and command rendering.

### Requirements Checklist

- Preserve existing client instruction commands when `source_checkout` is not
  provided.
- Preserve existing absolute explicit-checkout command rendering.
- Resolve a relative explicit `source_checkout` before rendering each
  per-client `apply_command`.
- Include the same resolved explicit-checkout command in the top-level next
  steps.
- Keep client instructions secret-free and otherwise schema-compatible.

### Implementation Steps

1. Add the focused failing MCP regression for relative explicit
   source-checkout client instructions.
2. Resolve explicit client-instruction `source_checkout` paths before passing
   them into env-file resolution and command rendering.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_resolves_relative_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6G_-HM

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` returns a normal MCP tool
result when `_run_setup` returns a `blocked` or `failed` first-run readiness
payload without raising. That makes host readiness blockers look successful at
the MCP protocol layer unless clients inspect the payload status.

Scope is limited to MCP setup status `isError` parity and its focused regression
test.

### Requirements Checklist

- Preserve successful and warning setup status responses as non-error MCP tool
  calls.
- Mark `awf_get_setup_status` responses as MCP errors when rendered readiness
  status is `blocked` or `failed`.
- Keep the existing setup status response payload shape and redaction behavior.
- Add a focused regression proving blocked and failed readiness payloads set
  `result.isError`.

### Implementation Steps

1. Add the focused failing MCP regression for blocked/failed setup status.
2. Change `_get_setup_status_result` to pass `is_error=True` only when rendered
   setup readiness status is `blocked` or `failed`.
3. Run the targeted regression and focused setup-tools test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.
