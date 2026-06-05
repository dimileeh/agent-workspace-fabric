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

## Review Repair: PRRT_kwDOSJAM6s6HceGm

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the existing missing-env blocked payload status, reason code,
  summary, details, and absence of apply commands: Complete.
- Preserve explicit `source_checkout` behavior for missing-env payloads:
  Complete.
- When the missing env file belongs to the persisted source checkout, render
  setup retry and start remediation commands with that checkout root: Complete.
- Avoid inferring a persisted source checkout when host config has no matching
  source-checkout metadata: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_persisted_source_env_rewrites_start_remediation -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_persisted_source_env_rewrites_start_remediation tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_unmatched_env_keeps_default_remediation tests/unit/mcp/test_setup_tools_client_integration.py::test_client_env_file_missing_source_checkout_ignores_absent_or_unreadable_config tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` remained `awf setup --client claude` instead of carrying
  the persisted checkout.
- Focused missing-env tests after the implementation change: 4 passed.
- Focused ruff: passed.
- Focused ruff format check: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HcTJa

Plan reference: `T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the existing reason-coded blocked payload shape, command rendering,
  `SETUP_READINESS_FAILED` reason code, and redacted `error_type` detail:
  Complete.
- Return a planning-specific summary for unexpected exceptions raised while
  building client config plans: Complete.
- Keep the later payload-transformation failure summary unchanged: Complete.
- Update the focused client-integration regression for the planning exception
  branch: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_has_planning_summary -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["summary"]` was
  `could not inspect existing client MCP configuration`.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HZ6Wt

### Requirement Status

- Preserve existing blocked missing-env payload status, reason code, summary,
  details, top-level setup command, next steps, and absence of apply commands:
  Complete.
- When explicit `source_checkout` is present, rewrite the issue remediation
  `related_command` to `awf start --source-checkout <resolved checkout>`:
  Complete.
- Preserve existing behavior when no explicit `source_checkout` is present:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["issues"][0]["remediation"]["related_command"]` was
  `awf start --source-checkout .` instead of the explicit checkout start
  command.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HZuDL

### Requirement Status

- Preserve the top-level `awf start` command rendering for explicit
  `source_checkout`: Complete.
- Rewrite source-checkout setup remediation commands to the resolved explicit
  `awf start --source-checkout ...` command: Complete.
- Continue preserving unrelated remediation commands and the
  `START_COMPOSE_ASSETS_MISSING` no-source-checkout exception: Complete.
- Add a focused regression for the structured issue remediation command:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["issues"][0]["remediation"]["related_command"]` remained
  `awf setup --source-checkout .`.
- Regression test after the implementation change: 1 passed.
- Adjacent focused tests for reason-coded bootstrap rewrites and the
  no-source-checkout compose-assets exception: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: python-coverage-shards (2) Stale MCP Coverage Node

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Keep the protected contract registry unchanged: Complete.
- Restore the exact pytest node ID referenced by the MCP parity coverage map:
  Complete.
- Make the restored node assert real secret-free MCP client-instruction
  behavior: Complete.
- Keep `tests/unit/mcp/test_setup_tools.py` under the 1500-line guardrail:
  Complete.
- Run focused repro and affected MCP checks only: Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py
```

Latest results:

- Focused contract smoke repro failed before the repair because
  `tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free`
  was not a collected pytest node ID.
- Focused contract smoke repro after the repair: 1 passed.
- Restored client-integration secret-free node: 1 passed.
- Line-limit guard: 1 passed.
- Affected MCP setup-tools module: 35 passed.
- Focused ruff: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HZNUj

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the existing `awf_start_local_service` failure payload shape and
  contextual command rewrite behavior: Complete.
- Include resolved start `inputs.settings` token fields in MCP redaction for
  bootstrap failure payloads: Complete.
- Include secret-keyed values from the selected start `inputs.service_env` in
  MCP redaction for bootstrap failure payloads: Complete.
- Add a focused regression using a non-token-shaped custom secret that would
  not be caught by generic provider-token patterns: Complete.
- Run focused tests/checks only: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `src/awf/mcp/server.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_redacts_selected_start_environment_secret_from_bootstrap_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/mcp/server.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/mcp/server.py
```

Latest results:

- Regression test failed before the implementation change because selected
  settings and secret-keyed environment values appeared in the rendered MCP
  payload.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: python-coverage-shards (8) Test File Line Limit

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Keep all existing MCP setup-tool behavioral assertions intact: Complete.
- Bring `tests/unit/mcp/test_setup_tools.py` under the 1500-line guardrail:
  Complete.
- Keep destination test files under the same guardrail: Complete.
- Avoid workflow, quality-gate, and protected configuration changes: Complete.
- Use focused local checks only: Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
```

Latest results:

- The maintainability repro failed before the test move with
  `tests/unit/mcp/test_setup_tools.py: 1504`.
- After moving the client-integration tests, line counts are
  `tests/unit/mcp/test_setup_tools.py: 1385` and
  `tests/unit/mcp/test_setup_tools_client_integration.py: 612`.
- Focused maintainability guard: 1 passed.
- Affected MCP test modules: 50 passed.
- Focused ruff: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HYot6

### Requirement Status

- Preserve the top-level contextual MCP start command: Complete.
- Preserve `START_COMPOSE_ASSETS_MISSING` remediation commands when no explicit
  source checkout was provided by the caller: Complete.
- Continue rewriting ordinary start retry remediations, including explicit
  source-checkout start invocations: Complete.
- Add a focused regression for the asset-missing package/install lane:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["issues"][0]["remediation"]["related_command"]` was
  `awf start --rebuild` instead of the catalog remediation
  `awf start --source-checkout .`.
- Focused asset-missing and port-conflict start remediation tests after the
  implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Write Exception Guard

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve `FileExistsError` handling as `PROJECT_PROFILE_EXISTS`: Complete.
- Preserve known writer exception messages that include the exception type only:
  Complete.
- Convert unexpected writer exceptions into a generic `PROJECT_INIT_FAILED`
  response with safe `project_path` and `force` details: Complete.
- Log unexpected writer exceptions with project path and force context:
  Complete.
- Document that bridge re-export assignments are import-time attribute
  captures: Complete.
- Add focused regression coverage for the repaired writer failure path:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `src/awf/cli/first_run_mcp_bridge.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_unexpected_write_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Latest results:

- Regression test failed before the implementation change because the raw
  `TypeError` escaped as a FastMCP `ToolError` containing the writer exception
  detail.
- Regression test after the implementation change: 1 passed.
- Focused project-profile MCP test file: 14 passed.
- Bridge import-contract smoke tests: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HYP7k

### Requirement Status

- Preserve the existing top-level start command rewrite behavior: Complete.
- Rewrite nested issue `remediation.related_command` values that point at
  `awf start...` to the contextual start command: Complete.
- Preserve non-start remediation related commands: Complete.
- Add a focused regression for a reason-coded bootstrap failure with explicit
  start context: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reports_structured_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["issues"][0]["remediation"]["related_command"]` was `awf start`
  while the top-level command included `--rebuild`, `--timeout-seconds`, and
  `--source-checkout`.
- Focused start-failure MCP tests after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Client Next-Step Rewrite

### Requirement Status

- Preserve replacement of only the first setup command in a next-step string:
  Complete.
- Replace full dry-run provider-selector commands instead of only the dry-run
  prefix: Complete.
- Keep existing client command rewrites using the shared setup-command regex:
  Complete.
- Add a focused regression for the dangling-provider case: Complete.
- Treat the setup-status ignored-argument note as already satisfied by the
  current `_value` parameter name: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_instruction_reason_coded_next_step_rewrites_first_command_only -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the rewrite
  left `--provider github` dangling after the inserted client command.
- Regression test after the implementation change: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Probe Failure Message

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the structured `PROJECT_INIT_FAILED` response and safe redaction:
  Complete.
- Return a stage-specific sanitized message when the existing profile probe
  fails: Complete.
- Keep the preview-generation failure message unchanged: Complete.
- Update the existing focused regression for the probe-failure branch:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the probe
  branch still returned `could not build onboarding preview`.
- Focused preview/probe MCP tests after the implementation change: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Write-Before-Payload Failure

### Requirement Status

- Preserve the structured `PROJECT_INIT_FAILED` response and redaction behavior
  when onboarding payload construction fails: Complete.
- Prevent a failed payload build from leaving a newly written
  `.awf/workspace.yml` behind: Complete.
- Preserve idempotent retry behavior for the same failing call without
  requiring `force=true`: Complete.
- Add a focused regression proving write-mode payload failure does not leave the
  profile file and retries keep the original error code: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_payload_failure_does_not_leave_profile_or_change_retry_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_tools_are_registered tests/unit/mcp/test_setup_tools.py::test_setup_status_init_and_client_tools_offload_blocking_work -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the retry
  returned `PROJECT_PROFILE_EXISTS` after the first failed call wrote
  `.awf/workspace.yml`.
- Regression test after the implementation change: 1 passed.
- Focused project-profile MCP test file: 13 passed.
- Focused registration/offload MCP tests: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: Client Planner Exception Tests Depend On Ambient Env File

### Requirement Status

- Preserve the missing-env regression that asserts
  `START_COMPOSE_ASSETS_MISSING` wins before apply commands: Complete.
- Make the ValueError planner regression explicitly satisfy env-file
  resolution so it reaches the monkeypatched planner in clean checkouts:
  Complete.
- Make the unexpected-exception planner regression explicitly satisfy env-file
  resolution so it reaches the monkeypatched planner in clean checkouts:
  Complete.
- Keep secret-redaction assertions unchanged: Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

CI failure inspected:

- GitHub Actions run `27015318392`, job `python-coverage-shards (4)`, failed
  with two assertions in `tests/unit/mcp/test_setup_tools_client_integration.py`
  where the payload reason code was `START_COMPOSE_ASSETS_MISSING` instead of
  the expected `SETUP_READINESS_FAILED`.

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_value_error_is_readiness_failure tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools_client_integration.py
```

Latest results:

- Targeted planner-exception regressions: 2 passed.
- Focused client-integration test file: 14 passed.
- Focused ruff: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HWzCQ

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the explicit empty-client fast return without resolving the source
  checkout or env file: Complete.
- Preserve successful client-instruction payloads when the resolved env file
  exists: Complete.
- Block client-instruction responses when a valid source checkout resolves a
  missing root `.env`, before returning any client apply commands: Complete.
- Preserve explicit `source_checkout` in the returned retry command and
  next-step guidance: Complete.
- Add focused MCP regression coverage for the missing-env instruction path:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `src/awf/cli/first_run_mcp_bridge.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Latest results:

- Regression test failed before the implementation change because
  `awf_get_client_integration_instructions` returned success and advertised
  an apply command when the explicit source checkout had `.env.example` but no
  root `.env`.
- Regression test after the implementation change: 1 passed.
- Focused client-integration MCP test file: 14 passed.
- Bridge import-contract smoke test: 2 passed.
- Focused ruff: passed.
- Focused ruff format check: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HR9IF

### Requirement Status

- Preserve the existing reason-coded issue, details, redaction, and MCP error
  behavior for start input-resolution `SetupCheckError` failures: Complete.
- Keep top-level start command rendering for bare and explicit-checkout start
  retries: Complete.
- Rewrite copied reason-coded `next_steps` so the retry command matches the
  start-tool command and preserves explicit `source_checkout`: Complete.
- Add focused regression coverage for the bare and explicit-checkout
  `next_steps` paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_setup_check_input_resolution_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because
  `payload["next_steps"]` still told operators to re-run
  `awf setup --dry-run` on both bare and explicit-checkout start
  input-resolution `SetupCheckError` paths.
- Regression tests after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Onboarding Payload Guard

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve successful preview/write project-profile responses: Complete.
- Convert unexpected onboarding payload assembly failures into
  `PROJECT_INIT_FAILED` MCP errors: Complete.
- Keep returned details safe and generic with only project path and mode:
  Complete.
- Log the assembly failure with project path and mode context: Complete.
- Add focused regression coverage for the repaired failure path: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_payload_assembly_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `awf_initialize_project_profile` raised a raw `ToolError` containing the
  payload assembly exception text.
- Regression test after the implementation change: 1 passed.
- Focused project-profile MCP test file: 12 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Project Profile Message And Bridge Smoke Test

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the existing `PROJECT_PROFILE_EXISTS` error code, MCP error status,
  and safe detail fields: Complete.
- Keep the current "pass force=true" guidance when `force` is false: Complete.
- Return non-contradictory prose when `force` is true and the write still
  raises `FileExistsError`: Complete.
- Add a focused smoke test that imports `awf.cli.first_run_mcp_bridge` and
  verifies the public re-export names resolve: Complete.
- Do not edit unrelated setup/start/client behavior: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `tests/unit/mcp/test_setup_tools_import_contract.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_with_force_has_non_contradictory_message tests/unit/mcp/test_setup_tools_import_contract.py::test_first_run_mcp_bridge_public_exports_import -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_is_structured_mcp_error tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_with_force_has_non_contradictory_message tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The new `force=True` regression failed before implementation because the
  payload still said `project profile already exists; pass force=true to
  overwrite`.
- The direct bridge import/export smoke test passed and now guards the bridge
  re-export surface in focused MCP tests.
- Targeted regressions after implementation: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HRklw

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the explicit empty-client fast return and avoid source-checkout or
  env-file resolution: Complete.
- Do not return a generic mutating setup command for the no-op zero-client
  response: Complete.
- Keep the existing payload status, client list, env-file omission, and next
  steps unchanged: Complete.
- Add a focused regression assertion for the no-op command behavior: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before implementation because the empty-client payload
  returned `command="awf setup"`.
- Regression test after implementation: 1 passed.
- Focused client-integration MCP test file: 13 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HRcKZ

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve setup-status error payload shape and selected-provider command
  rendering: Complete.
- When no `source_checkout` is selected, rewrite `awf setup --dry-run`
  remediation text with the selected `--provider` values: Complete.
- Preserve existing `awf start` next-step text when no source checkout is
  selected: Complete.
- Add focused regression coverage for the repaired host-config error path:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["next_steps"]` still said to re-run `awf setup --dry-run` without
  the selected `--provider github`.
- Regression test after the implementation change: 1 passed.
- Regression plus setup-status source-checkout next-step coverage: 10 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Client Catch-All Reason Codes

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing redaction and generic `error_type` details for unexpected
  client planning failures: Complete.
- Map unexpected client planning failures to `SETUP_READINESS_FAILED`:
  Complete.
- Preserve existing redaction, command rewriting, and generic `error_type`
  details for unexpected response assembly failures: Complete.
- Map unexpected response assembly failures to `SETUP_READINESS_FAILED`:
  Complete.
- Update focused regression expectations for both paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Updated regressions failed before the implementation change because both
  paths returned `CLIENT_CONFIG_CONFLICT` instead of `SETUP_READINESS_FAILED`.
- Targeted regressions after the implementation change: 2 passed.
- Focused client-integration test file: 13 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HQ9mO

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing start option validation before input resolution: Complete.
- Preserve the sanitized error detail shape by exposing only the exception type:
  Complete.
- Return a first-run start payload instead of a generic MCP `ErrorResponse`:
  Complete.
- Render the retry command with accepted `rebuild`, `skip_agent_runtime_build`,
  `timeout_seconds`, and `source_checkout` context: Complete.
- Keep bootstrap execution skipped when input resolution fails: Complete.
- Add focused regression coverage for the repaired branch: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_runtime_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_called_process_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_value_error_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the guarded
  input-resolution branch returned a generic MCP `ErrorResponse` without
  `payload["status"]` or `payload["command"]`.
- Targeted regression after the implementation change: 1 passed.
- Adjacent input-resolution regressions after the implementation change:
  3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Private CLI Import Contract

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Stop importing underscore-prefixed symbols from `awf.cli.*` modules in
  `src/awf/mcp/setup_tools.py`: Complete.
- Expose explicit public bridge names for the CLI helper behavior the MCP layer
  intentionally shares: Complete.
- Preserve existing MCP setup-tools behavior and monkeypatch seams: Complete.
- Add a focused regression preventing private CLI imports from returning to the
  MCP setup-tools module: Complete.

### Evidence

Files changed:

- `src/awf/cli/first_run_mcp_bridge.py`
- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_import_contract.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py tests/unit/mcp/test_setup_tools.py::test_setup_tools_are_registered -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_status_init_and_client_tools_offload_blocking_work -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Latest results:

- New import-contract regression failed before the implementation change with
  18 private CLI imports from `src/awf/mcp/setup_tools.py`.
- Import-contract regression after the implementation change: 1 passed.
- Import-contract plus setup-tool registration test: 2 passed.
- Focused helper-seam test: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HQoJN

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve explicit `source_checkout` blocked responses and command rendering:
  Complete.
- When persisted checkout metadata fails with no explicit `source_checkout`,
  render the top-level command with the selected `--client` selectors:
  Complete.
- Rewrite the remediation next step to use the selected-client command instead
  of the generic `<client>` placeholder: Complete.
- Add focused regression coverage for the persisted-checkout failure path:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_persisted_source_checkout_failure_preserves_selected_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_persisted_source_checkout_failure_preserves_selected_clients tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_source_checkout_failure_preserves_explicit_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- New regression failed before the implementation change because
  `payload["command"]` was `awf setup` instead of
  `awf setup --client claude --client codex`.
- Targeted regression plus existing explicit-checkout regression after the
  implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523

### Requirement Status

- Preserve `SetupCheckError` and `SourceCheckoutError` handling exactly:
  Complete.
- Map planning-phase `OSError`, `RuntimeError`, and `ValueError` to
  `SETUP_READINESS_FAILED` instead of `CLIENT_CONFIG_CONFLICT`: Complete.
- Keep details generic and redacted with only the exception type surfaced:
  Complete.
- Preserve the command/next-step rewriting for the selected clients and
  explicit `source_checkout`: Complete.
- Add focused regression coverage for the system-level client-integration
  failure path: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_codex_invalid_home_override_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["reason_code"]` was `CLIENT_CONFIG_CONFLICT` for the invalid
  `CODEX_HOME`/home-expansion `RuntimeError` path.
- Regression test after the implementation change: 1 passed.
- Focused client-integration test file: 13 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HQd4E

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing start option validation, including rejecting simultaneous
  `rebuild=true` and `skip_agent_runtime_build=true`: Complete.
- Preserve existing bootstrap option wiring to `ServiceBootstrapOptions`:
  Complete.
- Render returned start payload commands with `--rebuild` when requested:
  Complete.
- Render returned start payload commands with `--skip-agent-runtime-build` when
  requested: Complete.
- Render returned start payload commands with `--timeout-seconds` when the MCP
  caller supplied a non-default timeout: Complete.
- Preserve explicit `source_checkout` command rendering together with any
  requested start options: Complete.
- Add focused regression coverage for the returned command: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reuses_bootstrap_and_is_idempotent tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_skip_agent_runtime_build_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -k start_local_service -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two command regressions failed before the implementation change because
  returned start payload commands dropped `--rebuild`, `--timeout-seconds`, and
  `--skip-agent-runtime-build`.
- Targeted regressions after the implementation change: 2 passed.
- Focused `start_local_service` subset: 17 passed, 12 deselected.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: Python Coverage Shard 8 File-Line Guard

### Requirement Status

- Keep all existing setup-tools test assertions and behavior coverage intact:
  Complete.
- Move a coherent subset of setup-status source-checkout tests into a separate
  owned test module so no first-party file exceeds 1500 lines: Complete.
- Do not weaken or edit the maintainability guard: Complete.
- Run the focused maintainability guard and the affected MCP setup-tools tests:
  Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py
```

Latest results:

- Focused line-limit guard: 1 passed.
- Affected MCP setup-tools test modules: 37 passed.
- Focused ruff: passed.

CI evidence:

- GitHub Actions run `26993845467`, job `python-coverage-shards (8)`, failed
  because `tests/unit/mcp/test_setup_tools.py` had 1770 lines and exceeded
  `MAX_FIRST_PARTY_FILE_LINES = 1500`.
- After the split, `tests/unit/mcp/test_setup_tools.py` has 1270 lines and
  `tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py` has 527
  lines.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HQJ6B

### Requirement Status

- Preserve the existing sanitized reason code, summary, issue details,
  redaction, and MCP error behavior for generic client plan construction
  failures: Complete.
- Preserve the same behavior for response assembly failures: Complete.
- Render generic client plan construction failures with a command that includes
  the selected client values and explicit `source_checkout` when provided:
  Complete.
- Render response assembly failures with the same selected-client and explicit
  checkout retry command: Complete.
- Add focused regression coverage for both repaired paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_oserror_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two updated regressions failed before the implementation change because
  both generic client-instruction failure paths returned
  `payload["command"] == "awf setup"`.
- Targeted regressions after the implementation change: 2 passed.
- Focused client-integration test file: 10 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Client Planning Fallback And Start Step Rewrites

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing structured client-instruction handling for
  `SetupCheckError`, `SourceCheckoutError`, `OSError`, `RuntimeError`, and
  `ValueError`: Complete.
- Convert any other unexpected client planning exception into the same
  sanitized `CLIENT_CONFIG_CONFLICT` response shape used for planning
  inspection failures: Complete.
- Preserve normal setup-status next-step rewriting for dry-run and start
  commands: Complete.
- Prevent later command substitutions from rewriting inside command text
  inserted by an earlier substitution: Complete.
- Add focused regression coverage for both repaired paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_rewrite_inserted_start_command_path -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_oserror_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_value_error_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_rewrite_inserted_start_command_path tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two new regressions failed before the implementation change:
  `KeyError` escaped through FastMCP, and the start-step rewrite duplicated
  `--source-checkout` inside an inserted path containing `awf start to`.
- Targeted regressions after the implementation change: 2 passed.
- Focused neighboring test set after the implementation change: 6 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HP6Qv

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing reason code, issue details, redaction, status, and MCP error
  behavior for client normalization and planning `SetupCheckError` failures:
  Complete.
- Render normalization errors with a top-level command that preserves the
  requested client selectors: Complete.
- Render planning errors with a top-level command that preserves normalized
  selected clients and resolved explicit `source_checkout`: Complete.
- Keep top-level next steps aligned with the client-instruction command instead
  of generic setup readiness commands: Complete.
- Add focused regression coverage for normalization and planning error paths:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_unknown_client_is_structured_error tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_setup_error_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
```

Latest results:

- The two updated regressions failed before the implementation change because
  both `SetupCheckError` paths returned `payload["command"] == "awf setup"`.
- Targeted regressions after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.
- Focused client-integration test file: 9 passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HP5RB

### Requirement Status

- Preserve the existing sanitized reason code, summary, issue details, redaction,
  and MCP error behavior for generic readiness probe failures: Complete.
- Preserve the same behavior for post-render setup-status transformation
  failures: Complete.
- Render both generic setup-status failure commands as `awf setup --dry-run`
  with the original provider selectors: Complete.
- Preserve explicit `source_checkout` in both dry-run retry commands and
  checkout-aware next steps: Complete.
- Add focused regression coverage for both generic setup-status failure paths:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted tests/unit/mcp/test_setup_tools.py::test_get_setup_status_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because both generic
  setup-status failure paths returned `payload["command"] == "awf setup"`.
- Regression tests after the implementation change: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HP5Q8

### Requirement Status

- Preserve the existing reason-coded issue, details, redaction, and MCP error
  behavior for start input-resolution `SetupCheckError` failures: Complete.
- Render bare start input-resolution `SetupCheckError` failures with
  `command="awf start"`: Complete.
- Preserve explicit `source_checkout` in that command as
  `awf start --source-checkout <path>`: Complete.
- Add focused regression coverage for the bare and explicit-checkout command
  paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_setup_check_input_resolution_failure_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -k start_local_service -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because
  `payload["command"]` was `awf setup` on both the bare and explicit-checkout
  start input-resolution `SetupCheckError` paths.
- Regression tests after the implementation change: 2 passed.
- Focused `start_local_service` subset: 16 passed, 20 deselected.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Write Errors And Empty Client Env File

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve the dedicated `PROJECT_PROFILE_EXISTS` response for
  `FileExistsError`: Complete.
- Convert `OSError`, `RuntimeError`, and `ValueError` from
  `write_workspace_profile` into sanitized `PROJECT_INIT_FAILED` MCP errors:
  Complete.
- Do not surface raw write exception text or token-like values in the MCP
  response: Complete.
- Preserve the explicit empty-client client-instruction fast return without
  source-checkout or env-file resolution: Complete.
- Document that `env_file` is present only when at least one client integration
  plan is returned: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `tests/unit/mcp/test_mcp_client_parity_docs.py`
- `docs/MCP_CLIENT_PARITY.md`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_runtime_and_value_errors_are_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_is_structured_mcp_error tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_runtime_and_value_errors_are_structured tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients tests/unit/mcp/test_mcp_client_parity_docs.py::test_first_run_setup_tools_are_documented_as_local_secret_free_mcp_surface -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/test_mcp_client_parity_docs.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- New project-profile write regression failed before the implementation change
  for both `RuntimeError` and `ValueError` because raw exceptions escaped
  through FastMCP.
- Project-profile write regression after implementation: 2 passed.
- Focused project-profile, empty-client, and docs contract set: 5 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HPtNg

### Requirement Status

- Preserve existing client SourceCheckoutError reason code, issue details,
  summary, and MCP error behavior: Complete.
- Preserve current generic blocked payload behavior when no explicit
  `source_checkout` is supplied: Complete.
- When an explicit `source_checkout` fails validation, render the top-level
  command with the selected clients and resolved `--source-checkout` path:
  Complete.
- Render the blocked next step with the same explicit-checkout remediation
  command so operators retry the checkout that failed: Complete.
- Add a focused regression proving the explicit checkout is preserved in the
  SourceCheckoutError path: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_source_checkout_failure_preserves_explicit_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_source_checkout_failure_preserves_explicit_command tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` was `awf setup` on the client-instruction
  `SourceCheckoutError` path.
- Regression test after the implementation change: 1 passed.
- Focused client-instruction tests: 2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HPsBy

### Requirement Status

- Preserve valid provider, client, consent, and persisted source-checkout
  metadata after a successful host config read: Complete.
- Omit `setup.config_path` when `default_host_setup_config_path()` is not
  resolvable on an explicit source-checkout status probe: Complete.
- Preserve the existing fallback to an empty config when host config reading
  itself fails for an explicit source-checkout status probe: Complete.
- Add a focused regression proving config metadata is not dropped when only
  the config path lookup fails: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_preserves_host_config_when_config_path_fails -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_preserves_host_config_when_config_path_fails tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["setup"]["plain_file_consent"]` was `False` after
  `default_host_setup_config_path()` failed, proving the loaded config had been
  dropped.
- Regression test after the implementation change: 1 passed.
- Regression plus neighboring setup-status config fallback tests after the
  implementation change: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HPsBt

### Requirement Status

- Preserve the existing structured host-config error payload, sanitized details,
  reason code, issue data, and MCP error behavior: Complete.
- Render the setup-status `HostSetupConfigError` command as
  `awf setup --dry-run` with the original provider selectors: Complete.
- Keep the top-level next step aligned with the setup-status dry-run command:
  Complete.
- Add a focused regression proving the host-config error path returns the
  matching dry-run status command: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` was `awf setup` on the setup-status
  `HostSetupConfigError` path.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HPbkf

### Requirement Status

- Preserve setup-status payload shape and existing safe metadata: Complete.
- When no `source_checkout` is supplied, render `command` as
  `awf setup --dry-run` plus any selected providers: Complete.
- When explicit `source_checkout` is supplied, preserve the existing
  `awf setup --dry-run --source-checkout <path>` rendering: Complete.
- Add or update a focused regression for selected provider preservation:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the
  no-checkout setup-status payload returned `command="awf setup"` instead of
  `command="awf setup --dry-run --provider github"`.
- Focused setup-status command tests after the implementation change:
  3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Success Path Safe Result

### Requirement Status

- Preserve normal setup-status and client-instruction success payloads:
  Complete.
- Convert unexpected setup-status success-path transformation exceptions into a
  sanitized `SETUP_READINESS_FAILED` MCP error response: Complete.
- Convert unexpected client-instruction success-path transformation exceptions
  into a sanitized `CLIENT_CONFIG_CONFLICT` MCP error response: Complete.
- Ensure raw exception text and token-like values are not returned in the MCP
  response: Complete.
- Add focused regressions for both guarded success paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_success_transformation_failure_is_structured_and_redacted tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_success_transformation_failure_is_structured_and_redacted tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- The two new regressions failed before the implementation change because raw
  transformation exceptions escaped through FastMCP.
- Targeted regressions after the implementation change: 2 passed.
- Focused normal-success plus regression test set: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HPThr

### Requirement Status

- Preserve the existing source-checkout validation failure payload shape,
  reason code, redaction, and MCP error behavior: Complete.
- When `source_checkout` is provided and validation raises
  `SourceCheckoutError`, render `payload["command"]` as
  `awf start --source-checkout <resolved path>`: Complete.
- Preserve the generic `awf start` command when no explicit `source_checkout`
  is supplied: Complete by construction; the command override helper is a no-op
  for `source_checkout=None`.
- Add a focused regression proving the validation-failure path preserves the
  explicit source checkout command: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_success_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` was `awf start` on explicit source-checkout validation
  failure.
- Regression test after the implementation change: 1 passed.
- Focused source-checkout command preservation tests: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Empty Client Command

### Requirement Status

- Preserve the explicit empty-client fast return and avoid source checkout or
  env-file resolution: Complete.
- Return a valid no-client command hint for the empty-client response:
  Complete.
- Keep the existing payload status, client list, env-file omission, and next
  steps unchanged: Complete.
- Add a focused regression assertion for the returned command field: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before implementation because the empty-client payload
  returned `command="awf setup --client"` instead of `command="awf setup"`.
- Regression test after implementation: 1 passed.
- Focused client-integration MCP test file: 8 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HO7lM

### Requirement Status

- Preserve existing `awf_start_local_service` behavior when `source_checkout` is
  not provided: Complete.
- When explicit `source_checkout` is provided and startup succeeds, render the
  resolved checkout in the payload command: Complete.
- When explicit `source_checkout` is provided and bootstrap fails after input
  resolution, render the resolved checkout in the structured first-run failure
  payload command: Complete.
- Keep existing bootstrap diagnostics, redaction, and env-migration metadata
  unchanged: Complete.
- Add focused regressions for the success and adjacent bootstrap-execution
  failure paths: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_success_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_path_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reuses_bootstrap_and_is_idempotent tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_success_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_reports_structured_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_path_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_called_process_error_is_structured -q
```

Latest results:

- Regression tests failed before implementation because success, structured
  bootstrap failure, and bootstrap-execution failure payloads all returned
  `command="awf start"` for explicit `source_checkout`.
- Regression tests after implementation: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.
- Nearby start-tool regression sweep: 7 passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523

Plan reference: `plans/T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve existing structured handling for source-checkout conflicts and
  ordinary input-resolution `HostSetupConfigError`, `OSError`, `RuntimeError`,
  and `ValueError` failures: Complete.
- Convert input-resolution `SetupCheckError` failures into reason-coded,
  redacted first-run MCP errors: Complete.
- Convert input-resolution `CalledProcessError` failures into the existing
  generic `START_INPUT_RESOLUTION_FAILED` MCP error without surfacing raw command
  text or stderr: Complete.
- Ensure pre-bootstrap failures do not call `run_service_bootstrap`: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_called_process_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change: `SetupCheckError`
  used the generic `START_INPUT_RESOLUTION_FAILED` path, and
  `CalledProcessError` escaped as an MCP `ToolError`.
- Regression tests after the implementation change: 2 passed.
- Focused setup-tools test file: 27 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HOuPN

### Requirement Status

- Preserve the empty-client selection response: Complete.
- Include each selected client in the top-level command when `source_checkout`
  is not provided: Complete.
- Preserve explicit `source_checkout` command rendering: Complete.
- Add a focused regression for the no-checkout selected-client command field:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` was `awf setup --client` instead of
  `awf setup --client claude --client codex`.
- Regression test after the implementation change: 1 passed.
- Focused client-integration MCP test file: 8 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HOlA0

### Requirement Status

- Preserve existing client instruction payloads when `source_checkout` is not
  provided: Complete. The helper returns the existing generic command for the
  no-explicit-checkout path.
- When explicit `source_checkout` is provided, include the resolved checkout in
  the top-level `command` field: Complete.
- Keep the top-level command executable for the selected clients rather than
  dropping the client selectors: Complete.
- Add a focused regression for the explicit source-checkout command field:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` remained `awf setup --client` without the explicit
  `--source-checkout` argument.
- Regression test after the implementation change: 1 passed.
- Focused client-integration MCP test file: 8 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HObk-

### Requirement Status

- Preserve the existing sanitized `START_BOOTSTRAP_EXECUTION_FAILED` payload for
  non-`ServiceBootstrapError` bootstrap exceptions: Complete.
- Include resolved `env_migration` metadata in that payload when startup inputs
  were already resolved: Complete.
- Keep input-resolution failures unchanged because they do not have resolved
  migration metadata: Complete.
- Add a focused regression for the bootstrap-path error branch: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change with
  `KeyError: 'env_migration'`.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HOWVn

### Requirement Status

- Preserve normal setup-status payloads with `setup.config_path` when the host
  setup config path is available: Complete.
- Preserve explicit `source_checkout` fallback behavior when host config
  resolution/read fails: Complete.
- Omit `setup.config_path` when the fallback path cannot safely resolve it
  instead of re-raising: Complete.
- Add a focused regression for explicit source checkout status when both the
  config read and default config path rendering fail: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `default_host_setup_config_path()` re-raised the guarded
  `HostSetupConfigError` and `mcp.call_tool` surfaced a `ToolError`.
- Regression test after the implementation change: 1 passed.
- Focused neighboring setup-status tests: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: Coverage Shard 8 Maintainability Guard

### Requirement Status

- Keep all existing MCP setup/start/init/client behavior assertions intact:
  Complete. Project-profile assertions were moved without changing expected
  payloads, error codes, or log assertions.
- Move a coherent group of tests from `tests/unit/mcp/test_setup_tools.py` into
  an owned sibling test module: Complete. Project-profile initialization tests
  now live in `tests/unit/mcp/test_setup_tools_project_profile.py`.
- Keep each touched first-party test file below 1500 lines: Complete.
  `test_setup_tools.py` is 1180 lines and
  `test_setup_tools_project_profile.py` is 358 lines.
- Reproduce and then pass the focused maintainability guard locally: Complete.
- Run focused MCP setup test coverage for the touched test modules only:
  Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_project_profile.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
wc -l tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
```

Latest results:

- Focused maintainability guard initially reproduced the CI failure with
  `tests/unit/mcp/test_setup_tools.py` at 1525 lines.
- Focused maintainability guard after the split: 1 passed.
- Touched MCP setup test modules: 32 passed.
- Focused ruff: passed.
- Line counts: `test_setup_tools.py` 1180,
  `test_setup_tools_project_profile.py` 358.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HOFU3

### Requirement Status

- Preserve bare `awf start` next-step rewriting for explicit checkout status
  probes: Complete.
- Rewrite existing `awf start --source-checkout ...` next-step commands to the
  explicit checkout being probed: Complete.
- Do not duplicate `--source-checkout` flags in returned next steps: Complete.
- Add a focused regression for an upstream `awf start --source-checkout ...`
  next step that names a different checkout: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because the returned
  next step still contained the upstream checkout path.
- Regression test after the implementation change: 1 passed.
- Adjacent setup-status command-rewrite tests: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Bootstrap Execution Failure Reason Code

Plan reference: `T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Preserve `START_INPUT_RESOLUTION_FAILED` for failures raised while resolving
  start inputs before `run_service_bootstrap()` is called: Complete.
- Report `CalledProcessError`, `OSError`, `RuntimeError`, and `ValueError`
  raised from `run_service_bootstrap()` with a dedicated bootstrap-execution
  reason code: Complete.
- Keep bootstrap execution failure payloads structured as first-run `awf start`
  failures and continue excluding raw exception detail from MCP output:
  Complete.
- Add or update focused regressions for bootstrap-time `RuntimeError` and
  `CalledProcessError`: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_called_process_error_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_runtime_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Updated bootstrap execution regressions failed before the implementation
  change because both paths still returned `START_INPUT_RESOLUTION_FAILED`.
- Bootstrap execution regressions after the implementation change: 2 passed.
- Neighboring input-resolution regressions after the implementation change:
  2 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Command Suffix And Preview Probe Logging

### Requirement Status

- Preserve existing setup-status rewrites for period, comma, semicolon,
  right-parenthesis, end-of-string, and `to` suffixes: Complete.
- Rewrite colon-terminated `awf setup --dry-run:` and `awf start:` next-step
  commands to include the explicit source checkout: Complete.
- Preserve the sanitized `PROJECT_INIT_FAILED` response for existing-profile
  probe failures and onboarding preview failures: Complete.
- Log existing-profile probe failures with a probe-specific message, while
  continuing to log preview failures with the preview-specific message:
  Complete.
- Add focused regressions for the colon rewrite and probe-specific logging:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression tests failed before the implementation change because
  colon-terminated setup/start next steps were not rewritten and the
  existing-profile probe failure used the preview-specific log message.
- Regression tests after the implementation change: 3 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: PRRT_kwDOSJAM6s6HM47u Bootstrap Preflight Path Expansion

### Requirement Status

- Preserve normal `ServiceBootstrapError` first-run failure handling: Complete.
- Treat an unexpandable work-dir path raised from `run_service_bootstrap()` as a
  structured `awf start` first-run failure instead of letting `RuntimeError`
  escape through FastMCP: Complete.
- Add focused MCP regression coverage for the unexpandable work-dir path case:
  Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reports_structured_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_runtime_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because FastMCP
  returned a `ToolError` wrapping the raw `RuntimeError`.
- Regression test after the implementation change: 1 passed.
- Neighboring start-service structured-failure checks: 4 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## CI Repair: Setup Tools Test File Line Limit

Plan reference: `T09_MCP_SETUP_TOOLS_PLAN.md`

### Requirement Status

- Keep every setup-tools test behavior and assertion intact: Complete.
- Split `tests/unit/mcp/test_setup_tools.py` so no first-party file exceeds
  the 1,500-line maintainability limit: Complete.
- Keep the existing coverage-registry node ID for the representative
  client-integration smoke test stable: Complete.
- Avoid weakening, skipping, or disabling the maintainability guard: Complete.
- Keep verification focused; broad AWF/GitHub validation remains managed after
  the agent phase: Complete.

### Evidence

Files changed:

- `tests/unit/mcp/test_setup_tools.py`
- `tests/unit/mcp/test_setup_tools_client_integration.py`
- `tests/unit/mcp/setup_tools_test_helpers.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/setup_tools_test_helpers.py
```

Latest results:

- Focused line-limit repro before the split failed with
  `tests/unit/mcp/test_setup_tools.py: 1721`.
- Focused line-limit guard after the split: 1 passed.
- Affected setup-tools test modules after the split: 38 passed.
- Registry reference smoke test after preserving the original client smoke node:
  1 passed.
- Focused ruff: passed.
- Current line counts: `test_setup_tools.py` 1,423 lines,
  `test_setup_tools_client_integration.py` 283 lines,
  `setup_tools_test_helpers.py` 37 lines.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.

## Review Repair: issue:4620143523 Next-Step Command Rewriting

### Requirement Status

- Preserve existing rewrites for known setup-status next-step command shapes:
  Complete.
- Do not rewrite `awf start` occurrences that already include trailing command
  arguments in the same shell token sequence: Complete.
- Do not duplicate `--source-checkout` flags in returned next steps: Complete.
- Add a focused regression for an upstream `awf start --source-checkout ...`
  next step: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_codex_invalid_home_override_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `_setup_status_next_steps` rewrote `awf start --source-checkout ...` into a
  command with duplicate `--source-checkout` flags.
- New regression plus existing setup-status next-step checks: 3 passed.
- Existing client-instructions `RuntimeError` structured-error regression:
  1 passed.
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

## Review Repair: PRRT_kwDOSJAM6s6HPk73

### Requirement Status

- Preserve the existing reason-coded setup-status error payload shape,
  redaction, reason code, issue details, and MCP error behavior: Complete.
- Render the setup-status `SetupCheckError` command as
  `awf setup --dry-run` with the original provider selectors: Complete.
- Preserve explicit `source_checkout` in that dry-run retry command: Complete.
- Keep setup-status provider-selector guidance read-only by rendering
  `awf setup --dry-run` in the top-level next step: Complete.
- Add a focused regression proving the early error path returns the matching
  dry-run status command: Complete.

### Evidence

Files changed:

- `src/awf/mcp/setup_tools.py`
- `tests/unit/mcp/test_setup_tools.py`
- `plans/T09_MCP_SETUP_TOOLS_PLAN.md`
- `plans/T09_MCP_SETUP_TOOLS_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_setup_check_error_returns_matching_dry_run_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Latest results:

- Regression test failed before the implementation change because
  `payload["command"]` was `awf setup` on the setup-status
  `SetupCheckError` path.
- Regression test after the implementation change: 1 passed.
- Focused ruff: passed.
- Focused mypy: passed.

Full AWF/GitHub validation and coverage gates were not run in the agent phase;
AWF owns broad validation, provenance, logs, timeouts, and merge gating after
agent completion.
