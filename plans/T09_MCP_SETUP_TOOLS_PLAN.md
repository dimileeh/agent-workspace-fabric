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
