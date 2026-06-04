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
