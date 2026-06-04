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
