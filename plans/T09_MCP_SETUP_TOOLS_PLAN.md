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

## Review Repair: PRRT_kwDOSJAM6s6HdUce

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` loses the persisted
source-checkout root when no explicit `source_checkout` argument is supplied and
`_resolve_start_source_checkout(None)` raises `SourceCheckoutError`. The
exception still carries the stale persisted checkout in `exc.root`, but the MCP
start error path passes `source_path=None` into remediation rewriting, leaving
the issue's catalog default `awf setup --source-checkout .`.

Scope is limited to the start-tool `SourceCheckoutError` branch and its focused
regression. Existing explicit `source_checkout` behavior and unrelated start
failure payloads remain unchanged.

### Requirements Checklist

- Preserve the `SourceCheckoutError` reason code, issue details, and MCP error
  response shape.
- Preserve existing explicit `source_checkout` command rendering.
- When no explicit `source_checkout` is supplied but `SourceCheckoutError.root`
  identifies the persisted checkout, render source-checkout remediation against
  that persisted checkout instead of `.`.
- Add a focused regression proving
  `issues[].remediation.related_command` targets the persisted checkout.

### Implementation Steps

1. Add the focused failing MCP regression for persisted source-checkout start
   validation failure.
2. In the start-tool `SourceCheckoutError` branch, use `exc.root` as the
   remediation checkout when no explicit source path is available.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_persisted_source_checkout_failure_uses_persisted_remediation_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6Hc2bA Persisted Stale Checkout Remediation

### Problem Statement And Scope

The review reports that when `awf_get_client_integration_instructions` fails
on stale persisted source-checkout metadata without an explicit
`source_checkout`, the MCP wrapper rewrites the top-level command and next
step to the selected client invocation but leaves the nested issue
`remediation.related_command` at the shared source-checkout catalog command.

Scope is limited to source-checkout blocked payloads returned by MCP client
integration instructions and their focused regression. Start-service
remediation rewriting and unrelated client setup failures stay unchanged.

### Requirements Checklist

- Preserve selected client command rendering for stale persisted checkout
  failures without explicit `source_checkout`.
- Rewrite source-checkout issue remediation commands to the same selected
  client instruction command.
- Preserve the existing explicit `source_checkout` source-checkout failure
  behavior.
- Add a focused regression for the persisted stale nested remediation command.

### Implementation Steps

1. Extend the persisted source-checkout failure regression to assert
   `issues[].remediation.related_command` and confirm it fails first.
2. Rewrite source-checkout issue remediation commands in the client blocked
   payload wrapper independent of whether an explicit checkout path is present.
3. Run the targeted regression and focused lint/type checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_persisted_source_checkout_failure_preserves_selected_clients -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6Hctfz

### Problem Statement And Scope

The review reports that explicit `source_checkout` validation failures in
`awf_get_client_integration_instructions` preserve the explicit checkout in the
top-level command and next step, but leave the nested issue remediation command
at the default `awf setup --source-checkout .`.

Scope is limited to the explicit client source-checkout failure payload wrapper
and its focused regression. Existing env-file-missing remediation rewriting and
client instruction success paths remain unchanged.

### Requirements Checklist

- Preserve the top-level blocked payload command and next step for explicit
  client source-checkout validation failures.
- Rewrite nested issue remediation commands for source-checkout remediation
  reason codes to the same explicit client instruction command.
- Preserve default remediation commands when no explicit `source_checkout` is
  available.
- Add a focused regression proving `issues[].remediation.related_command`
  preserves the explicit checkout path.

### Implementation Steps

1. Extend the existing explicit-checkout client-instruction failure regression to
   assert the nested remediation command and confirm it fails.
2. Reuse the existing start-issue remediation rewrite helper in the explicit
   client source-checkout blocked payload wrapper.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_source_checkout_failure_preserves_explicit_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HcTJa

### Problem Statement And Scope

The review reports that unexpected exceptions while planning MCP client
integration instructions return `SETUP_READINESS_FAILED` with stale summary
text about inspecting existing client MCP configuration. This can mislead
operators because plan construction failures are not the same as local client
configuration inspection conflicts.

Scope is limited to the generic planning exception branch in
`_client_integration_instructions_result` and its focused regression.

### Requirements Checklist

- Preserve the existing reason-coded blocked payload shape, command rendering,
  `SETUP_READINESS_FAILED` reason code, and redacted `error_type` detail.
- Return a planning-specific summary for unexpected exceptions raised while
  building client config plans.
- Keep the later payload-transformation failure summary unchanged.
- Update the focused client-integration regression for the planning exception
  branch.

### Implementation Steps

1. Update the existing planning exception regression to expect the
   planning-specific summary and confirm it fails before implementation.
2. Change only the generic planning exception summary in
   `src/awf/mcp/setup_tools.py`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_has_planning_summary -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HZ6Wt Missing Env Remediation Command

### Problem Statement And Scope

The review reports that MCP client instructions preserve an explicit
`source_checkout` in the missing-env top-level command and next steps, but leave
the structured `START_COMPOSE_ASSETS_MISSING` issue remediation command as the
reason-catalog default `awf start --source-checkout .`. MCP clients following
that structured command can start the current directory instead of creating the
`.env` for the validated checkout.

Scope is limited to the client-integration missing-env payload path and its
focused regression.

### Requirements Checklist

- Preserve existing blocked missing-env payload status, reason code, summary,
  details, top-level setup command, next steps, and absence of apply commands.
- When explicit `source_checkout` is present, rewrite the issue remediation
  `related_command` to `awf start --source-checkout <resolved checkout>`.
- Preserve existing behavior when no explicit `source_checkout` is present.

### Implementation Steps

1. Extend the existing missing-source-env regression to assert the structured
   issue remediation command for an explicit checkout.
2. Update `_client_env_file_missing_payload_with_explicit_command` to rewrite
   issue remediations with the explicit checkout start command.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HZuDL

### Problem Statement And Scope

The review reports that `awf_start_local_service` preserves an explicit
`source_checkout` in the top-level retry command when source-checkout
validation fails, but leaves the issue remediation catalog command at
`awf setup --source-checkout .`. MCP clients that follow the structured issue
remediation would validate the current directory instead of the checkout that
just failed.

Scope is limited to source-checkout validation failures returned through
`awf_start_local_service` and the helper that decides which issue remediation
commands may be rewritten. Unrelated remediation commands such as service status
must stay unchanged.

### Requirements Checklist

- Preserve the top-level `awf start` command rendering for explicit
  `source_checkout`.
- Rewrite source-checkout setup remediation commands to the resolved explicit
  `awf start --source-checkout ...` command.
- Continue preserving unrelated remediation commands and the
  `START_COMPOSE_ASSETS_MISSING` no-source-checkout exception.
- Add a focused regression for the structured issue remediation command.

### Implementation Steps

1. Extend the existing explicit source-checkout validation-failure regression
   to assert the structured issue remediation command.
2. Update the remediation rewrite predicate so source-checkout catalog setup
   commands are eligible when an explicit checkout path is present.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HceGm Persisted Missing Env Remediation

### Problem Statement And Scope

The review reports that when MCP client instruction setup relies on persisted
source-checkout metadata and the checkout root `.env` is missing,
`_resolve_client_env_file(None, True)` raises `ClientEnvFileMissingError` while
the MCP-layer `source_path` remains `None`. The missing-env payload then keeps
the catalog `awf start --source-checkout .` remediation command instead of
pointing at the persisted checkout whose env file is missing.

Scope is limited to the client-integration missing-env error path and its
focused regression.

### Requirements Checklist

- Preserve the existing missing-env blocked payload status, reason code,
  summary, details, and absence of apply commands.
- Preserve explicit `source_checkout` behavior for missing-env payloads.
- When the missing env file belongs to the persisted source checkout, render
  setup retry and start remediation commands with that checkout root.
- Avoid inferring a persisted source checkout when host config has no matching
  source-checkout metadata.

### Implementation Steps

1. Add a focused failing MCP regression for a persisted source checkout with a
   missing root `.env`.
2. Resolve the missing-env payload checkout from the explicit source path or
   matching persisted host-setup metadata.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_persisted_source_env_rewrites_start_remediation tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_unmatched_env_keeps_default_remediation tests/unit/mcp/test_setup_tools_client_integration.py::test_client_env_file_missing_source_checkout_ignores_absent_or_unreadable_config tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: python-coverage-shards (2) Stale MCP Coverage Node

### Problem Statement And Scope

`python-coverage-shards (2)` fails in
`tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference`
because the MCP parity coverage registry references
`tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free`,
but the client-integration tests were moved into
`tests/unit/mcp/test_setup_tools_client_integration.py` during the line-limit
repair. The contract registry is a quality-gate file outside this workspace's
declared owned paths, so this repair restores the referenced executable node
from the owned MCP test file instead of editing the registry.

### Requirements Checklist

- Keep the protected contract registry unchanged.
- Restore the exact pytest node ID referenced by the MCP parity coverage map.
- Make the restored node assert real secret-free MCP client-instruction
  behavior.
- Keep `tests/unit/mcp/test_setup_tools.py` under the 1500-line guardrail.
- Run focused repro and affected MCP checks only; full AWF/GitHub validation
  remains managed by AWF after agent completion.

### Implementation Steps

1. Confirm the focused contract smoke repro fails on the stale node.
2. Add a minimal behavior test named
   `test_client_integration_instructions_are_secret_free` to
   `tests/unit/mcp/test_setup_tools.py`.
3. Run the focused contract smoke repro, the restored test node, and line-limit
   guard.
4. Update the validation document with focused evidence.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HZNUj

### Problem Statement And Scope

The review reports that `awf_start_local_service` can embed raw
`ServiceBootstrapError` diagnostics from a selected source-checkout startup
environment whose `.env` values differ from the MCP server startup settings.
The existing MCP redaction closure only knows the server-level secrets captured
when `build_mcp_server(...)` was called, so a bootstrap stderr/stdout message
that echoes a non-token-shaped selected `.env` secret could be returned.

Scope is limited to the MCP setup-tool start failure path for resolved
bootstrap inputs. It should not change bootstrap behavior, first-run payload
classification, or CLI output.

### Requirements Checklist

- Preserve the existing `awf_start_local_service` failure payload shape and
  contextual command rewrite behavior.
- Include resolved start `inputs.settings` token fields in MCP redaction for
  bootstrap failure payloads.
- Include secret-keyed values from the selected start `inputs.service_env` in
  MCP redaction for bootstrap failure payloads.
- Add a focused regression using a non-token-shaped custom secret that would
  not be caught by generic provider-token patterns.
- Run focused tests/checks only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

### Implementation Steps

1. Add a focused failing regression where `run_service_bootstrap(...)` raises a
   `ServiceBootstrapError` whose stderr contains one selected settings token
   and one selected secret-keyed environment value not present in the
   server-level MCP settings.
2. Extend the MCP `safe_result` callback to accept optional per-call exact
   secrets while keeping existing call sites compatible.
3. Collect selected start secrets from resolved `inputs.settings` and
   `inputs.service_env`, and pass them only when rendering the bootstrap
   failure result.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_redacts_selected_start_environment_secret_from_bootstrap_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/mcp/server.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/mcp/server.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: python-coverage-shards (8) Test File Line Limit

### Problem Statement And Scope

GitHub Actions run `27020086110`, job `python-coverage-shards (8)`, fails
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
because `tests/unit/mcp/test_setup_tools.py` has 1504 lines, exceeding the
1500-line first-party file limit.

Scope is limited to moving coherent client-integration tests from the oversized
general setup-tools test module into the existing
`tests/unit/mcp/test_setup_tools_client_integration.py` module. No production
behavior changes are needed.

### Requirements Checklist

- Keep all existing MCP setup-tool behavioral assertions intact.
- Bring `tests/unit/mcp/test_setup_tools.py` under the 1500-line guardrail.
- Keep destination test files under the same guardrail.
- Do not change workflow, quality-gate, or protected configuration files.
- Run only focused repro/verification commands; broad AWF/GitHub validation
  remains owned by AWF after agent completion.

### Implementation Steps

1. Reproduce the maintainability failure locally with the single failing test.
2. Move the client-instruction tests currently in `test_setup_tools.py` into
   `test_setup_tools_client_integration.py`, adding only the imports needed
   there.
3. Re-run the maintainability guardrail and the affected MCP test modules.
4. Update the T09 validation artifact with evidence from the focused checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HYot6

### Problem Statement And Scope

The PR review reports that `_start_payload_with_command` now rewrites every
issue remediation command matching `awf start...` to the exact failed MCP start
command. For `START_COMPOSE_ASSETS_MISSING`, the reason catalog intentionally
uses `awf start --source-checkout .` when the caller did not pass an explicit
source checkout, because that is the structured recovery command for package or
install lanes that lack Compose assets.

Scope is limited to preserving that asset-missing source-checkout remediation
while keeping the existing contextual rewrite for ordinary start retry commands.

### Requirements Checklist

- Preserve the top-level contextual MCP start command.
- Preserve `START_COMPOSE_ASSETS_MISSING` remediation commands when no explicit
  source checkout was provided by the caller.
- Continue rewriting ordinary start retry remediations, including explicit
  source-checkout start invocations.
- Add a focused regression for the asset-missing package/install lane.

### Implementation Steps

1. Add a focused failing MCP regression where bootstrap raises
   `SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND` without a caller `source_checkout`.
2. Pass the explicit source-checkout context into the start issue remediation
   rewrite helper.
3. Skip the rewrite only for `START_COMPOSE_ASSETS_MISSING` when the caller did
   not provide `source_checkout`.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Write Exception Guard

### Problem Statement And Scope

The review reports that `_initialize_project_profile_result` defensively wraps
existing profile probing, onboarding preview, and onboarding payload assembly,
but the `write_workspace_profile(...)` branch only handles known writer
exceptions. Unexpected serialization or YAML failures could therefore escape the
MCP tool instead of returning a structured, redacted project-init error.

The review also notes that `first_run_mcp_bridge` re-export assignments capture
attributes at import time rather than acting as live aliases. Scope is limited
to documenting that maintenance constraint beside the existing bridge note.

### Requirements Checklist

- Preserve `FileExistsError` handling as `PROJECT_PROFILE_EXISTS`.
- Preserve known writer exception messages that include the exception type only.
- Convert unexpected writer exceptions into a generic `PROJECT_INIT_FAILED`
  response with safe `project_path` and `force` details.
- Log unexpected writer exceptions with project path and force context.
- Document that bridge re-export assignments are import-time attribute captures.
- Add focused regression coverage for the repaired writer failure path.

### Implementation Steps

1. Add a focused failing regression that makes `write_workspace_profile(...)`
   raise an unexpected exception after payload construction succeeds.
2. Add a catch-all writer exception handler that logs and returns the generic
   safe project-init error response.
3. Add the bridge maintenance comment about import-time attribute capture.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_unexpected_write_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HYP7k Start Remediation Command

### Problem Statement And Scope

The review reports that `awf_start_local_service` rewrites the top-level start
payload command when callers pass start context such as `source_checkout`,
`rebuild`, or a custom timeout, but leaves per-issue start remediation commands
from the reason catalog unchanged. A reason-coded bootstrap failure such as
`START_PORT_CONFLICT` can therefore render `remediation.related_command` as
plain `awf start` even though the failed invocation was more specific.

Scope is limited to preserving start invocation context in nested issue
remediation commands that are themselves start retry commands. Diagnostic
remediation commands such as `awf service status` or service logs stay unchanged.

### Requirements Checklist

- Preserve the existing top-level start command rewrite behavior.
- Rewrite nested issue `remediation.related_command` values that point at
  `awf start...` to the contextual start command.
- Preserve non-start remediation related commands.
- Add a focused regression for a reason-coded bootstrap failure with explicit
  start context.

### Implementation Steps

1. Add the focused failing MCP regression for nested related-command rewriting
   on a reason-coded start bootstrap failure.
2. Extend `_start_payload_with_command` to rebuild issue remediation payloads
   whose related command is an `awf start` command with the contextual command.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reports_structured_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Client Next-Step Rewrite

### Problem Statement And Scope

The review reports that `_client_instruction_reason_coded_next_step` rewrites
setup commands with ordered substring replacements while the analogous start
path uses `_START_REASON_CODED_SETUP_COMMAND_PATTERN`. A next-step template
containing `awf setup --dry-run --provider github` can therefore leave the
provider selector dangling after rewriting only the `awf setup --dry-run`
prefix.

Scope is limited to making the client-instruction reason-coded next-step helper
use the shared setup-command regex and adding a focused regression for the
provider-selector case. The setup-status ignored-argument note is already
satisfied by the current `_value` parameter name.

### Requirements Checklist

- Preserve replacement of only the first setup command in a next-step string.
- Replace full dry-run provider-selector commands instead of only the dry-run
  prefix.
- Keep existing client command rewrites using the shared setup-command regex.
- Add a focused regression for the dangling-provider case.

### Implementation Steps

1. Add the focused failing regression row to the client-instruction next-step
   rewrite test.
2. Change `_client_instruction_reason_coded_next_step` to use
   `_START_REASON_CODED_SETUP_COMMAND_PATTERN.sub(..., count=1)`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_instruction_reason_coded_next_step_rewrites_first_command_only -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Write-Before-Payload Failure

### Problem Statement And Scope

The PR review reports that `_initialize_project_profile_result` writes
`.awf/workspace.yml` before building the MCP response payload. If payload
construction fails after the write, the first call returns `PROJECT_INIT_FAILED`
while leaving a profile on disk; a plain retry with `write_profile=true` and
`force=false` can then return `PROJECT_PROFILE_EXISTS`.

Scope is limited to the project-profile MCP initialization write/payload
ordering and its focused regression.

### Requirements Checklist

- Preserve the structured `PROJECT_INIT_FAILED` response and redaction behavior
  when onboarding payload construction fails.
- Prevent a failed payload build from leaving a newly written
  `.awf/workspace.yml` behind.
- Preserve idempotent retry behavior for the same failing call without requiring
  `force=true`.
- Add a focused regression proving write-mode payload failure does not leave the
  profile file and retries keep the original error code.

### Implementation Steps

1. Add the focused failing MCP regression for write-mode payload assembly
   failure and retry behavior.
2. Reorder `_initialize_project_profile_result` so the response payload is
   constructed before `write_workspace_profile(...)` runs for write mode.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_payload_failure_does_not_leave_profile_or_change_retry_error -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: Client Planner Exception Tests Depend On Ambient Env File

### Problem Statement And Scope

GitHub Actions run `27015318392` fails in `python-coverage-shards (4)` because
two MCP client-integration tests expect planner exceptions to be rendered as
`SETUP_READINESS_FAILED`, but in CI the tool returns
`START_COMPOSE_ASSETS_MISSING` before the monkeypatched planner is called. The
tests pass in this workspace only because a root `.env` exists here; CI uses a
clean checkout where the default client env file is absent.

Scope is limited to making those two tests environment-independent. The MCP
runtime behavior remains unchanged: missing env files still block before client
apply instructions, while planner exceptions are still redacted readiness
failures once the env-file precondition is satisfied.

### Requirements Checklist

- Preserve the missing-env regression that asserts
  `START_COMPOSE_ASSETS_MISSING` wins before apply commands.
- Make the ValueError planner regression explicitly satisfy env-file
  resolution so it reaches the monkeypatched planner in clean checkouts.
- Make the unexpected-exception planner regression explicitly satisfy env-file
  resolution so it reaches the monkeypatched planner in clean checkouts.
- Keep secret-redaction assertions unchanged.

### Implementation Steps

1. Update the two planner-exception tests to monkeypatch
   `_resolve_client_env_file` to a test-local env path.
2. Run the two targeted regressions and the focused client-integration test
   file.
3. Run focused lint on the touched test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_value_error_is_readiness_failure tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools_client_integration.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HWzCQ

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` resolves
the client MCP env file with `require_existing=False`. For an explicit or
persisted valid source checkout that only has `.env.example`, the instruction
tool can therefore return success and advertise apply commands that immediately
block in the CLI non-dry-run path because the root `.env` does not exist.

Scope is limited to making MCP client-integration instructions enforce the same
env-file existence prerequisite before returning apply commands, and to reuse
the existing CLI missing-env blocked payload shape instead of returning a
generic readiness failure.

### Requirements Checklist

- Preserve the explicit empty-client fast return without resolving the source
  checkout or env file.
- Preserve successful client-instruction payloads when the resolved env file
  exists.
- Block client-instruction responses when a valid source checkout resolves a
  missing root `.env`, before returning any client apply commands.
- Preserve explicit `source_checkout` in the returned retry command and
  next-step guidance.
- Add focused MCP regression coverage for the missing-env instruction path.

### Implementation Steps

1. Add a focused failing MCP regression for a valid explicit source checkout
   that has `.env.example` but no root `.env`.
2. Expose the existing CLI `ClientEnvFileMissingError` and missing-env payload
   through the first-run MCP bridge.
3. Require the resolved client env file to exist before building MCP
   instruction plans, and return the existing missing-env blocked payload with
   the explicit client-instruction retry command.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_missing_source_env_blocks_before_apply_commands -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev ruff format --check tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HR9IF

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` rewrites the top-level
command to `awf start` when bootstrap input resolution raises `SetupCheckError`,
but leaves copied reason-code `next_steps` pointing at `awf setup --dry-run`.
Operators copying the remediation then get setup-status guidance that does not
match the start-tool context.

Scope is limited to start local-service reason-coded `SetupCheckError`
remediation command rendering.

### Requirements Checklist

- Preserve the existing reason-coded issue, details, redaction, and MCP error
  behavior for start input-resolution `SetupCheckError` failures.
- Keep top-level start command rendering for bare and explicit-checkout start
  retries.
- Rewrite copied reason-coded `next_steps` so the retry command matches the
  start-tool command and preserves explicit `source_checkout`.
- Add focused regression coverage for the bare and explicit-checkout
  `next_steps` paths.

### Implementation Steps

1. Extend the existing start input-resolution `SetupCheckError` regressions to
   assert start-context `next_steps`.
2. Update the start payload command wrapper to transform copied setup retry
   commands inside top-level `next_steps`.
3. Run the targeted regressions and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_setup_check_input_resolution_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Onboarding Payload Guard

### Problem Statement And Scope

The review reports that `_initialize_project_profile_result` guards existing
profile probing, onboarding preview, and profile writing, but calls
`_init_project_onboarding_payload(...)` outside any defensive handler. An
unexpected preview shape or payload-builder failure could therefore escape the
MCP tool instead of returning a structured project-init error response.

Scope is limited to wrapping project-profile onboarding payload assembly and
adding focused regression coverage. Existing success payloads, write behavior,
and earlier error handling stay unchanged.

### Requirements Checklist

- Preserve successful preview/write project-profile responses.
- Convert unexpected onboarding payload assembly failures into
  `PROJECT_INIT_FAILED` MCP errors.
- Keep returned details safe and generic with only project path and mode.
- Log the assembly failure with project path and mode context.
- Add focused regression coverage for the repaired failure path.

### Implementation Steps

1. Add a focused failing regression that makes onboarding payload assembly
   raise after preview succeeds.
2. Wrap `_init_project_onboarding_payload(...)` in a defensive handler.
3. Return a structured `_error_result(...)` with safe project path and mode
   details on assembly failure.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_payload_assembly_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Project Profile Message And Bridge Smoke Test

### Problem Statement And Scope

The review reports two remaining first-run MCP setup tool concerns:

- `awf_initialize_project_profile` returns contradictory prose if
  `write_workspace_profile(..., force=True)` still raises `FileExistsError`.
- The public first-run MCP bridge re-exports private CLI helper symbols, but no
  focused smoke test imports the bridge and verifies that the exported surface
  is currently available.

Scope is limited to the project-profile `FileExistsError` message branch and a
focused bridge import/export smoke test. The bridge module implementation,
tool schemas, payload shapes, and existing redaction behavior stay unchanged.

### Requirements Checklist

- Preserve the existing `PROJECT_PROFILE_EXISTS` error code, MCP error status,
  and safe detail fields.
- Keep the current "pass force=true" guidance when `force` is false.
- Return non-contradictory prose when `force` is true and the write still
  raises `FileExistsError`.
- Add a focused smoke test that imports `awf.cli.first_run_mcp_bridge` and
  verifies the public re-export names resolve.
- Do not edit unrelated setup/start/client behavior.

### Implementation Steps

1. Add or update focused regressions for the false-force and true-force
   `FileExistsError` message branches.
2. Add a focused bridge import/export smoke test beside the existing MCP import
   contract test.
3. Change only the `FileExistsError` message selection in
   `_initialize_project_profile_result`.
4. Run the targeted project-profile and import-contract tests plus focused
   lint/type checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_is_structured_mcp_error tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_with_force_has_non_contradictory_message tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HRklw

### Problem Statement And Scope

The review reports that `awf_get_client_integration_instructions` returns a
generic `command="awf setup"` for explicit `clients: []` requests. That response
is intentionally a zero-client no-op that skips source-checkout and env-file
resolution, so a copied setup command would run the normal provider setup flow
instead of reproducing the no-op request.

Scope is limited to the explicit empty-client success payload and its focused
regression assertion.

### Requirements Checklist

- Preserve the explicit empty-client fast return and avoid source-checkout or
  env-file resolution.
- Do not return a generic mutating setup command for the no-op zero-client
  response.
- Keep the existing payload status, client list, env-file omission, and next
  steps unchanged.
- Add a focused regression assertion for the no-op command behavior.

### Implementation Steps

1. Update the existing explicit empty-client regression to reject the generic
   setup command.
2. Omit the `command` field from the explicit empty-client success payload.
3. Run the targeted regression and focused client-integration checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HRcKZ

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` rebuilds the structured
`command` field with selected `--provider` values on error paths, but leaves
copied `next_steps` unchanged when no `source_checkout` is selected. Operators
who copy those remediation commands can therefore re-run a broader dry-run
than the structured command indicates.

Scope is limited to setup-status next-step command rewriting. Existing
source-checkout command rewriting, provider-unknown guidance, redaction, and
payload shape stay unchanged.

### Requirements Checklist

- Preserve setup-status error payload shape and selected-provider command
  rendering.
- When no `source_checkout` is selected, rewrite `awf setup --dry-run`
  remediation text with the selected `--provider` values.
- Preserve existing `awf start` next-step text when no source checkout is
  selected.
- Add focused regression coverage for the repaired host-config error path.

### Implementation Steps

1. Update the focused host-config error regression to require selected
   providers in copied setup dry-run next steps.
2. Change setup-status next-step rewriting so setup dry-run commands are
   rewritten even without `source_checkout`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Client Catch-All Reason Codes

### Problem Statement And Scope

The review reports that two generic `except Exception` handlers in
`_client_integration_instructions_result` classify unexpected internal failures
as `CLIENT_CONFIG_CONFLICT`. That reason code is reserved for real existing MCP
client configuration conflicts, so callers can be directed toward the wrong
diagnostic path.

Scope is limited to the two unexpected client-integration catch-all handlers.
Dedicated `SetupCheckError`, `SourceCheckoutError`, `OSError`, `RuntimeError`,
`ValueError`, and actual conflict responses remain unchanged.

### Requirements Checklist

- Preserve existing redaction and generic `error_type` details for unexpected
  client planning failures.
- Map unexpected client planning failures to `SETUP_READINESS_FAILED`.
- Preserve existing redaction, command rewriting, and generic `error_type`
  details for unexpected response assembly failures.
- Map unexpected response assembly failures to `SETUP_READINESS_FAILED`.
- Update focused regression expectations for both paths.

### Implementation Steps

1. Update the two focused client-integration regressions to expect
   `SETUP_READINESS_FAILED`.
2. Change both generic `except Exception` handlers in
   `_client_integration_instructions_result` to use
   `SETUP_READINESS_FAILED`.
3. Run the targeted regressions and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HQ9mO

### Problem Statement And Scope

The PR review reports that the guarded `awf_start_local_service` input
resolution error branch returns a generic MCP `ErrorResponse` when
`_resolve_start_bootstrap_inputs_for_mcp` raises `CalledProcessError`,
`HostSetupConfigError`, `OSError`, `RuntimeError`, or `ValueError`. The adjacent
start failure branches already return first-run payloads with the rendered
retry command, so this branch drops accepted start options such as `--rebuild`,
custom timeouts, and explicit `--source-checkout`.

Scope is limited to this guarded start input-resolution failure branch and its
focused regressions. The response must remain credential-safe and must not run
the bootstrap when input resolution fails.

### Requirements Checklist

- Preserve existing start option validation before input resolution.
- Preserve the sanitized error detail shape by exposing only the exception type.
- Return a first-run start payload instead of a generic MCP `ErrorResponse`.
- Render the retry command with accepted `rebuild`, `skip_agent_runtime_build`,
  `timeout_seconds`, and `source_checkout` context.
- Keep bootstrap execution skipped when input resolution fails.
- Add focused regression coverage for the repaired branch.

### Implementation Steps

1. Update the focused start input-resolution regression to require a first-run
   payload with the caller's start command context.
2. Change `_start_input_resolution_error_result` to build a credential-safe
   first-run payload with `START_INPUT_RESOLUTION_FAILED`.
3. Thread the accepted start options into the guarded input-resolution failure
   branch.
4. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Private CLI Import Contract

### Problem Statement And Scope

The review notes that `src/awf/mcp/setup_tools.py` imports underscore-prefixed
helpers from CLI modules. That makes the MCP first-run surface depend on private
CLI implementation details, so a future CLI refactor could break MCP without a
clear compatibility contract.

Scope is limited to the MCP setup-tools dependency boundary. The MCP tools must
continue to delegate to the same first-run setup/start/init/client behavior and
preserve existing test seams, response payloads, and redaction behavior.

### Requirements Checklist

- Stop importing underscore-prefixed symbols from `awf.cli.*` modules in
  `src/awf/mcp/setup_tools.py`.
- Expose explicit public bridge names for the CLI helper behavior the MCP layer
  intentionally shares.
- Preserve existing MCP setup-tools behavior and monkeypatch seams.
- Add a focused regression preventing private CLI imports from returning to the
  MCP setup-tools module.

### Implementation Steps

1. Add a focused failing import-contract regression for
   `src/awf/mcp/setup_tools.py`.
2. Add a public bridge module in the CLI package for the shared first-run helper
   behavior used by MCP.
3. Update `setup_tools.py` to import the public bridge names while keeping its
   local helper seams stable.
4. Run the targeted import-contract regression and focused lint/type checks for
   the touched files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_import_contract.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_setup_tools_are_registered tests/unit/mcp/test_setup_tools.py::test_setup_status_init_and_client_tools_offload_blocking_work -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py tests/unit/mcp/test_setup_tools_import_contract.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py src/awf/cli/first_run_mcp_bridge.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HQoJN

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` preserves
the selected-client command when an explicit `source_checkout` fails, but leaves
the CLI helper's generic `awf setup` / `<client>` remediation unchanged when the
same `SourceCheckoutError` comes from stale persisted source-checkout metadata.

Scope is limited to the MCP client-integration `SourceCheckoutError` response.
Existing issue details, reason codes, redaction, and explicit checkout behavior
must remain unchanged.

### Requirements Checklist

- Preserve explicit `source_checkout` blocked responses and command rendering.
- When persisted checkout metadata fails with no explicit `source_checkout`,
  render the top-level command with the selected `--client` selectors.
- Rewrite the remediation next step to use the selected-client command instead
  of the generic `<client>` placeholder.
- Add focused regression coverage for the persisted-checkout failure path.

### Implementation Steps

1. Add a focused failing MCP client-integration regression where
   `_resolve_client_env_file(None, False)` raises `SourceCheckoutError` after
   client normalization.
2. Apply the existing selected-client command rewrite for all client
   `SourceCheckoutError` responses, including when `source_checkout` is absent.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_persisted_source_checkout_failure_preserves_selected_clients -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523

### Problem Statement And Scope

The PR review reports that the `awf_get_client_integration_instructions`
planning path maps OS/runtime failures, such as home-directory resolution
errors, to `CLIENT_CONFIG_CONFLICT`. That gives MCP clients conflict-oriented
guidance even when the operator needs to fix the local environment.

Scope is limited to the client-integration instructions planning error mapping
and focused regression coverage. Existing redaction behavior and true
client-config conflict behavior must remain unchanged.

### Requirements Checklist

- Preserve `SetupCheckError` and `SourceCheckoutError` handling exactly.
- Map planning-phase `OSError`, `RuntimeError`, and `ValueError` to
  `SETUP_READINESS_FAILED` instead of `CLIENT_CONFIG_CONFLICT`.
- Keep details generic and redacted with only the exception type surfaced.
- Preserve the command/next-step rewriting for the selected clients and
  explicit `source_checkout`.
- Add focused regression coverage for the system-level client-integration
  failure path.

### Implementation Steps

1. Update the focused client-integration regression expectation to require
   `SETUP_READINESS_FAILED` for a system-level planning failure.
2. Add a dedicated planning-phase handler before the catch-all in
   `_client_integration_instructions_result`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_codex_invalid_home_override_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HQd4E

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` honors MCP start options
when constructing `ServiceBootstrapOptions`, but rewrites every returned
first-run payload command to only `awf start` or
`awf start --source-checkout <path>`. Operators copying that command can retry
without requested non-default start options such as forced rebuild, runtime-build
skip, or a custom readiness timeout.

Scope is limited to preserving accepted MCP start options in returned
`awf_start_local_service` first-run payload commands.

### Requirements Checklist

- Preserve existing start option validation, including rejecting simultaneous
  `rebuild=true` and `skip_agent_runtime_build=true`.
- Preserve existing bootstrap option wiring to `ServiceBootstrapOptions`.
- Render returned start payload commands with `--rebuild` when requested.
- Render returned start payload commands with `--skip-agent-runtime-build` when
  requested.
- Render returned start payload commands with `--timeout-seconds` when the MCP
  caller supplied a non-default timeout.
- Preserve explicit `source_checkout` command rendering together with any
  requested start options.
- Add focused regression coverage for the returned command.

### Implementation Steps

1. Extend the existing start-service option regression to assert that the
   returned success command preserves rebuild, timeout, and source-checkout
   values.
2. Add a focused regression for the skip-runtime-build option because it is
   mutually exclusive with rebuild.
3. Replace the checkout-only command override helper with a start-command helper
   that renders the accepted option values.
4. Thread the accepted start options into each start first-run payload path.
5. Run targeted regressions and focused lint/type checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_reuses_bootstrap_and_is_idempotent tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_skip_agent_runtime_build_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: Python Coverage Shard 8 File-Line Guard

### Problem Statement And Scope

GitHub Actions run `26993845467` failed `python-coverage-shards (8)` in
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
because `tests/unit/mcp/test_setup_tools.py` grew to 1770 lines, exceeding the
first-party file limit of 1500 lines.

Scope is limited to splitting the oversized MCP setup-tools test module into
focused test modules under `tests/unit/mcp` while preserving existing behavior
coverage and assertions.

### Requirements Checklist

- Keep all existing setup-tools test assertions and behavior coverage intact.
- Move a coherent subset of setup-status source-checkout tests into a separate
  owned test module so no first-party file exceeds 1500 lines.
- Do not weaken or edit the maintainability guard.
- Run the focused maintainability guard and the affected MCP setup-tools tests.

### Implementation Steps

1. Move setup-status source-checkout focused tests from
   `tests/unit/mcp/test_setup_tools.py` into a new focused module under
   `tests/unit/mcp`.
2. Keep imports minimal in both modules after the split.
3. Run the line-limit guard and the affected setup-tools test modules.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_setup_status_source_checkout.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HQJ6B

### Problem Statement And Scope

The PR review reports that generic client-instruction failures during client
plan construction or response assembly return `_reason_coded_payload(...)`
unchanged. Those generic payloads advertise `awf setup` retry commands instead
of preserving the caller's selected `--client` values and explicit
`--source-checkout`, unlike the `SetupCheckError` path.

Scope is limited to the existing generic `awf_get_client_integration_instructions`
error branches and focused regressions for command and next-step rendering.

### Requirements Checklist

- Preserve the existing sanitized reason code, summary, issue details,
  redaction, and MCP error behavior for generic client plan construction
  failures.
- Preserve the same behavior for response assembly failures.
- Render generic client plan construction failures with a command that includes
  the selected client values and explicit `source_checkout` when provided.
- Render response assembly failures with the same selected-client and explicit
  checkout retry command.
- Add focused regression coverage for both repaired paths.

### Implementation Steps

1. Extend the focused client-integration generic planning failure regression to
   assert command and next-step preservation.
2. Extend the focused client-integration response assembly failure regression to
   assert command and next-step preservation.
3. Replace the two generic client-integration `_reason_coded_payload(...)`
   returns with `_client_instruction_reason_coded_payload(...)`.
4. Run the targeted regressions and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_oserror_is_generic tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Client Planning Fallback And Start Step Rewrites

### Problem Statement And Scope

The review reports two remaining safety gaps in
`src/awf/mcp/setup_tools.py`:

- unexpected exceptions raised while building client integration plans can
  escape the first planning block instead of returning a structured
  credential-safe MCP error;
- the setup-status next-step rewrite runs multiple sequential regex
  substitutions, so later substitutions can rewrite text inside a newly
  inserted path.

Scope is limited to those two behaviors and focused regressions.

### Requirements Checklist

- Preserve existing structured client-instruction handling for
  `SetupCheckError`, `SourceCheckoutError`, `OSError`, `RuntimeError`, and
  `ValueError`.
- Convert any other unexpected client planning exception into the same
  sanitized `CLIENT_CONFIG_CONFLICT` response shape used for planning
  inspection failures.
- Preserve normal setup-status next-step rewriting for dry-run and start
  commands.
- Prevent later command substitutions from rewriting inside command text
  inserted by an earlier substitution.
- Add focused regression coverage for both repaired paths.

### Implementation Steps

1. Add a client-integration regression where `build_client_config_plan` raises
   an unexpected exception such as `KeyError` containing token-like text.
2. Add a setup-status regression where the explicit replacement
   `source_checkout` path contains text that matches the bare-start pattern.
3. Add a final broad `Exception` fallback to the first client planning block
   that returns sanitized conflict metadata.
4. Rewrite setup-status commands in a single pass so replacement command text is
   not scanned by another command pattern.
5. Run targeted regressions plus focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_unexpected_exception_is_generic tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_rewrite_inserted_start_command_path -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HP6Qv

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` returns
`_reason_coded_payload(...)` unchanged when client normalization or selected
client planning raises `SetupCheckError`. That generic payload renders
`awf setup` / `awf setup --dry-run` remediation instead of the matching
`awf setup --client ...` instruction request the MCP caller made.

Scope is limited to the client-integration `SetupCheckError` branch and focused
regressions for command and next-step rendering.

### Requirements Checklist

- Preserve existing reason code, issue details, redaction, status, and MCP error
  behavior for client normalization and planning `SetupCheckError` failures.
- Render normalization errors with a top-level command that preserves the
  requested client selectors.
- Render planning errors with a top-level command that preserves normalized
  selected clients and resolved explicit `source_checkout`.
- Keep top-level next steps aligned with the client-instruction command instead
  of generic setup readiness commands.
- Add focused regression coverage for normalization and planning error paths.

### Implementation Steps

1. Extend focused client-integration regressions for unknown-client and planning
   `SetupCheckError` command/next-step rendering.
2. Wrap the existing reason-coded payload in a client-instruction command helper
   before returning it from the `SetupCheckError` branch.
3. Run targeted regressions and focused lint/type checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_unknown_client_is_structured_error tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_planning_setup_error_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HP5RB

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` still returns the generic
`_reason_coded_payload(...)` unchanged for non-`SetupCheckError` readiness probe
failures and post-render transformation failures. Those sanitized generic paths
therefore advertise the mutating `awf setup` command and omit explicit
`source_checkout` retry context, unlike the setup-status-specific early error
paths.

Scope is limited to the two existing generic setup-status error branches and
focused regressions for command and next-step rendering. Redaction and structured
error details must stay unchanged.

### Requirements Checklist

- Preserve the existing sanitized reason code, summary, issue details, redaction,
  and MCP error behavior for generic readiness probe failures.
- Preserve the same behavior for post-render setup-status transformation
  failures.
- Render both generic setup-status failure commands as `awf setup --dry-run`
  with the original provider selectors.
- Preserve explicit `source_checkout` in both dry-run retry commands and
  checkout-aware next steps.
- Add focused regression coverage for both generic setup-status failure paths.

### Implementation Steps

1. Extend the focused generic setup-status failure regressions to assert dry-run
   command and explicit-checkout next-step rendering.
2. Replace the two generic setup-status `_reason_coded_payload(...)` returns
   with `_setup_status_reason_coded_payload(...)`.
3. Run the targeted regressions and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted tests/unit/mcp/test_setup_tools.py::test_get_setup_status_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HP5Q8

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` returns
`_reason_coded_payload(...)` unchanged when start bootstrap input resolution
raises `SetupCheckError`. That renders the generic `awf setup` command on a
start-tool failure, and it does not preserve an explicit `source_checkout` in
the command field.

Scope is limited to the start local-service `SetupCheckError` branch and
focused regressions for its command rendering.

### Requirements Checklist

- Preserve the existing reason-coded issue, details, redaction, and MCP error
  behavior for start input-resolution `SetupCheckError` failures.
- Render bare start input-resolution `SetupCheckError` failures with
  `command="awf start"`.
- Preserve explicit `source_checkout` in that command as
  `awf start --source-checkout <path>`.
- Add focused regression coverage for the bare and explicit-checkout command
  paths.

### Implementation Steps

1. Extend the existing start input-resolution `SetupCheckError` regression to
   assert the bare start command.
2. Add a focused explicit-checkout regression for the same error branch.
3. Wrap the existing reason-coded payload in a start-command helper before
   returning it from the `SetupCheckError` branch.
4. Run the targeted regressions and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_setup_check_input_resolution_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Write Errors And Empty Client Env File

### Problem Statement And Scope

The review reports two MCP setup-tool edge cases:

- `awf_initialize_project_profile` only catches `FileExistsError` and
  `OSError` from `write_workspace_profile`, letting `RuntimeError` and
  `ValueError` escape the MCP tool boundary.
- The explicit empty-client response from
  `awf_get_client_integration_instructions` omits `env_file`, while non-empty
  client responses include it.

Scope is limited to the project-profile write error boundary and documenting
the empty-client `env_file` contract. Existing regression coverage requires the
empty-client fast path to avoid source-checkout and env-file resolution, so this
repair documents `env_file` as optional for zero selected clients instead of
changing that behavior.

### Requirements Checklist

- Preserve the dedicated `PROJECT_PROFILE_EXISTS` response for
  `FileExistsError`.
- Convert `OSError`, `RuntimeError`, and `ValueError` from
  `write_workspace_profile` into sanitized `PROJECT_INIT_FAILED` MCP errors.
- Do not surface raw write exception text or token-like values in the MCP
  response.
- Preserve the explicit empty-client client-instruction fast return without
  source-checkout or env-file resolution.
- Document that `env_file` is present only when at least one client integration
  plan is returned.

### Implementation Steps

1. Add focused failing project-profile write regressions for `RuntimeError` and
   `ValueError`.
2. Broaden the non-FileExists project-profile write exception handler to match
   the comparable MCP setup-tool pattern.
3. Update the MCP client-parity documentation and docs assertion for the
   optional empty-client `env_file` contract.
4. Run targeted project-profile, client-integration, docs, lint, and type
   checks for the touched files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_runtime_and_value_errors_are_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_file_exists_is_structured_mcp_error tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_write_runtime_and_value_errors_are_structured tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients tests/unit/mcp/test_mcp_client_parity_docs.py::test_first_run_setup_tools_are_documented_as_local_secret_free_mcp_surface -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/test_mcp_client_parity_docs.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HPtNg

### Problem Statement And Scope

The PR review reports that
`awf_get_client_integration_instructions` drops an explicit
`source_checkout` from the SourceCheckoutError remediation path. The success
path renders `--source-checkout <resolved-path>` in the command and per-client
apply commands, but the failure path returns the shared client blocked payload
unchanged, leaving a generic `awf setup` command and
`awf setup --client <client>` next step.

Scope is limited to MCP client-instruction SourceCheckoutError command and
next-step rendering.

### Requirements Checklist

- Preserve existing client SourceCheckoutError reason code, issue details,
  summary, and MCP error behavior.
- Preserve current generic blocked payload behavior when no explicit
  `source_checkout` is supplied.
- When an explicit `source_checkout` fails validation, render the top-level
  command with the selected clients and resolved `--source-checkout` path.
- Render the blocked next step with the same explicit-checkout remediation
  command so operators retry the checkout that failed.
- Add a focused regression proving the explicit checkout is preserved in the
  SourceCheckoutError path.

### Implementation Steps

1. Add the focused failing MCP regression for client-instruction
   SourceCheckoutError command rendering.
2. Update the MCP client-instruction error branch to wrap the existing blocked
   payload with explicit-checkout command and next-step context only when the
   user supplied `source_checkout`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_source_checkout_failure_preserves_explicit_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HPsBy

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` drops valid host setup config
metadata on the explicit `source_checkout` path when `read_host_setup_config()`
succeeds but `default_host_setup_config_path()` raises
`HostSetupConfigError`. The current shared `try` treats both failures as a
signal to replace the loaded config with an empty `HostSetupConfig()`.

Scope is limited to preserving the already loaded host config while omitting
`setup.config_path` when only the default path resolution fails.

### Requirements Checklist

- Preserve valid provider, client, consent, and persisted source-checkout
  metadata after a successful host config read.
- Omit `setup.config_path` when `default_host_setup_config_path()` is not
  resolvable on an explicit source-checkout status probe.
- Preserve the existing fallback to an empty config when host config reading
  itself fails for an explicit source-checkout status probe.
- Add a focused regression proving config metadata is not dropped when only
  the config path lookup fails.

### Implementation Steps

1. Add the focused failing MCP regression for successful config read followed
   by default config path failure.
2. Split host config reading from config path lookup so path errors only clear
   `config_path`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_preserves_host_config_when_config_path_fails -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HPsBt

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` still returns the generic
`_reason_coded_payload(...)` unchanged when host setup config parsing fails
after the dry-run readiness probe. That payload renders the mutating
`awf setup` command, so an MCP operator copying it can run setup instead of the
matching read-only `awf setup --dry-run ...` status check.

Scope is limited to the setup-status `HostSetupConfigError` branch and focused
regression coverage for the returned command.

### Requirements Checklist

- Preserve the existing structured host-config error payload, sanitized details,
  reason code, issue data, and MCP error behavior.
- Render the setup-status `HostSetupConfigError` command as
  `awf setup --dry-run` with the original provider selectors.
- Keep the top-level next step aligned with the setup-status dry-run command.
- Add a focused regression proving the host-config error path returns the
  matching dry-run status command.

### Implementation Steps

1. Update the existing setup-status host-config-error regression to assert the
   returned command and next-step guidance.
2. Wrap the existing `HostSetupConfigError` payload in the setup-status
   reason-coded helper before returning it from `_get_setup_status_result`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HPbkf

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` runs setup readiness in
dry-run mode but, when no `source_checkout` is supplied, returns the rendered
`_run_setup` command unchanged. That command can be `awf setup`, so an MCP
operator copying the read-only status payload can run the mutating setup flow
instead of the matching dry-run status check.

Scope is limited to rendering the setup-status `command` field with the dry-run
setup command helper for both no-checkout and explicit-checkout calls.

### Requirements Checklist

- Preserve setup-status payload shape and existing safe metadata.
- When no `source_checkout` is supplied, render `command` as
  `awf setup --dry-run` plus any selected providers.
- When explicit `source_checkout` is supplied, preserve the existing
  `awf setup --dry-run --source-checkout <path>` rendering.
- Add or update a focused regression for selected provider preservation.

### Implementation Steps

1. Update the existing no-checkout setup-status regression so it expects the
   returned command to be the selected-provider dry-run command.
2. Extend `_setup_status_dry_run_command` to omit `--source-checkout` when no
   checkout path is present, and have `_setup_status_command` use it for every
   setup-status payload.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Success Path Safe Result

### Problem Statement And Scope

The review reports that `_get_setup_status_result` and
`_client_integration_instructions_result` handle dependency failures through
structured `safe_result` responses, but then build their success payloads
outside those guards. If a post-dependency transformation helper raises, the
exception can escape through FastMCP instead of returning a redacted MCP error
payload.

Scope is limited to guarding the success-path payload transformations for those
two MCP tools. Existing dependency-specific exception handling, payload schemas,
and normal success behavior remain unchanged.

### Requirements Checklist

- Preserve normal setup-status and client-instruction success payloads.
- Convert unexpected setup-status success-path transformation exceptions into a
  sanitized `SETUP_READINESS_FAILED` MCP error response.
- Convert unexpected client-instruction success-path transformation exceptions
  into a sanitized `CLIENT_CONFIG_CONFLICT` MCP error response.
- Ensure raw exception text and token-like values are not returned in the MCP
  response.
- Add focused regressions for both guarded success paths.

### Implementation Steps

1. Add focused failing regressions that force setup-status and
   client-instruction post-dependency transformation helpers to raise.
2. Wrap only the success payload-building sections in narrow generic exception
   guards that return existing sanitized first-run error payloads.
3. Run the targeted regressions and focused lint/type checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_success_transformation_failure_is_structured_and_redacted tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HPThr

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` drops the explicit
`source_checkout` command override when checkout validation fails inside
`_resolve_start_bootstrap_inputs_for_mcp`. The returned source-checkout failure
payload still renders the generic `awf start` command, so copying it retries a
different startup context from the one the MCP caller requested.

Scope is limited to preserving the explicit source checkout command on the
source-checkout validation failure path for `awf_start_local_service`.

### Requirements Checklist

- Preserve the existing source-checkout validation failure payload shape,
  reason code, redaction, and MCP error behavior.
- When `source_checkout` is provided and validation raises
  `SourceCheckoutError`, render `payload["command"]` as
  `awf start --source-checkout <resolved path>`.
- Preserve the generic `awf start` command when no explicit `source_checkout`
  is supplied.
- Add a focused regression proving the validation-failure path preserves the
  explicit source checkout command.

### Implementation Steps

1. Add the focused failing regression for `awf_start_local_service` explicit
   source-checkout validation failure.
2. Wrap `_source_checkout_failure_payload(exc)` with the existing
   `_start_payload_with_source_checkout_command(..., source_path)` helper in
   the `SourceCheckoutError` branch.
3. Run the targeted regression and focused lint for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_success_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Empty Client Command

### Problem Statement And Scope

The review reports that the explicit empty-client path in
`awf_get_client_integration_instructions` returns `command="awf setup --client"`.
That string is not a valid CLI invocation because `--client` requires an
argument. Scope is limited to the empty-client early return and its focused
regression.

### Requirements Checklist

- Preserve the explicit empty-client fast return and avoid source checkout or
  env-file resolution.
- Return a valid no-client command hint for the empty-client response.
- Keep the existing payload status, client list, env-file omission, and next
  steps unchanged.
- Add a focused regression assertion for the returned command field.

### Implementation Steps

1. Extend the existing explicit empty-client regression to assert the returned
   command.
2. Change only the empty-client payload command to the valid no-client setup
   command.
3. Run the targeted regression and focused client-integration checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HO7lM

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` starts from an explicit
`source_checkout` but returns first-run payloads with `command="awf start"` on
success and the adjacent bootstrap-execution failure path. Copying that command
would rerun default or persisted assets instead of the checkout the MCP tool just
validated and started.

Scope is limited to MCP start payload command rendering for explicit
`source_checkout` values and focused MCP regressions.

### Requirements Checklist

- Preserve existing `awf_start_local_service` behavior when `source_checkout` is
  not provided.
- When explicit `source_checkout` is provided and startup succeeds, render the
  resolved checkout in the payload command.
- When explicit `source_checkout` is provided and bootstrap fails after input
  resolution, render the resolved checkout in the structured first-run failure
  payload command.
- Keep existing bootstrap diagnostics, redaction, and env-migration metadata
  unchanged.
- Add focused regressions for the success and adjacent bootstrap-execution
  failure paths.

### Implementation Steps

1. Add failing MCP regressions that assert explicit-checkout start success and
   structured bootstrap failure payloads use `awf start --source-checkout ...`.
2. Add a small MCP helper that overrides only the first-run payload `command`
   when a resolved explicit checkout path is available.
3. Thread the resolved MCP checkout path into the success and bootstrap-execution
   failure payload paths.
4. Run the targeted regressions and focused checks for the changed MCP files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_success_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_bootstrap_path_failure_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523

### Problem Statement And Scope

The PR review reports that the input-resolution phase of
`_start_local_service_result` catches a narrower exception set than comparable
MCP setup tools. In particular, a `SetupCheckError` or `CalledProcessError`
raised while resolving startup inputs can either lose reason-coded first-run
context or escape the MCP tool boundary instead of returning the intended
structured, redacted response.

Scope is limited to the pre-bootstrap `awf_start_local_service` input
resolution error boundary and focused regressions for the two omitted exception
types.

### Requirements Checklist

- Preserve existing structured handling for source-checkout conflicts and
  ordinary input-resolution `HostSetupConfigError`, `OSError`, `RuntimeError`,
  and `ValueError` failures.
- Convert input-resolution `SetupCheckError` failures into reason-coded,
  redacted first-run MCP errors.
- Convert input-resolution `CalledProcessError` failures into the existing
  generic `START_INPUT_RESOLUTION_FAILED` MCP error without surfacing raw command
  text or stderr.
- Ensure pre-bootstrap failures do not call `run_service_bootstrap`.

### Implementation Steps

1. Add focused regressions for `SetupCheckError` and `CalledProcessError` thrown
   during start input resolution.
2. Extend `_start_local_service_result`'s input-resolution exception handling to
   cover those exception types using the existing safe payload helpers.
3. Run the targeted regressions and focused checks for the changed setup-tools
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_setup_check_input_resolution_failure_is_reason_coded tests/unit/mcp/test_setup_tools.py::test_start_local_service_called_process_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HOuPN

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` builds a
top-level `command` containing selected clients when an explicit
`source_checkout` is provided, but falls back to the incomplete
`awf setup --client` command when no checkout is provided. Operators or MCP
clients copying that field cannot apply the same selected client plan.

Scope is limited to rendering the selected clients in the top-level client
instruction command for the no-explicit-checkout path. Empty explicit client
selection, per-client `apply_command`, next-step text, redaction, and explicit
checkout behavior stay unchanged.

### Requirements Checklist

- Preserve the empty-client selection response.
- Include each selected client in the top-level command when `source_checkout`
  is not provided.
- Preserve explicit `source_checkout` command rendering.
- Add a focused regression for the no-checkout selected-client command field.

### Implementation Steps

1. Add the focused failing MCP regression for selected-client top-level command
   rendering without `source_checkout`.
2. Reuse the existing client command builder for both checkout and no-checkout
   selected-client paths.
3. Run the targeted regression and focused client-integration test file.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_are_secret_free -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HOlA0

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` includes
an explicit `--source-checkout` in each per-client `apply_command` and in the
returned next steps, but the top-level `command` field remains the generic
`awf setup --client`. Operators or agents that copy only `command` can therefore
apply client setup against the default or persisted checkout instead of the
checkout used to build the MCP instruction plan.

Scope is limited to making the top-level client-instruction command preserve
the explicit source checkout selection. Per-client payloads, next-step text,
secret redaction, and behavior without `source_checkout` stay unchanged.

### Requirements Checklist

- Preserve existing client instruction payloads when `source_checkout` is not
  provided.
- When explicit `source_checkout` is provided, include the resolved checkout in
  the top-level `command` field.
- Keep the top-level command executable for the selected clients rather than
  dropping the client selectors.
- Add a focused regression for the explicit source-checkout command field.

### Implementation Steps

1. Extend the explicit source-checkout client-instruction regression so it
   expects `payload["command"]` to include the resolved `--source-checkout`.
2. Add a small helper that renders the top-level client-instruction command
   from selected clients and the resolved checkout.
3. Use that helper only on the non-empty selected-client path.
4. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py::test_client_integration_instructions_preserve_explicit_source_checkout_apply_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HObk-

### Problem Statement And Scope

The PR review reports that `awf_start_local_service` includes resolved
`env_migration` metadata on the normal structured bootstrap failure path, but
drops that metadata when `run_service_bootstrap()` raises a non-
`ServiceBootstrapError` that is converted into a synthetic first-run bootstrap
execution failure.

Scope is limited to preserving env-migration metadata on the existing MCP
bootstrap-path error response. The sanitized bootstrap error shape and redaction
behavior stay unchanged.

### Requirements Checklist

- Preserve the existing sanitized `START_BOOTSTRAP_EXECUTION_FAILED` payload for
  non-`ServiceBootstrapError` bootstrap exceptions.
- Include resolved `env_migration` metadata in that payload when startup inputs
  were already resolved.
- Keep input-resolution failures unchanged because they do not have resolved
  migration metadata.
- Add a focused regression for the bootstrap-path error branch.

### Implementation Steps

1. Extend the existing bootstrap-path error regression so it expects the same
   env-migration metadata included by the structured bootstrap failure path.
2. Thread `inputs.env_migration` through `_start_bootstrap_path_error_result`.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HOWVn

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` intentionally falls back to
default host setup config when `source_checkout` is explicit and
`read_host_setup_config()` cannot resolve/read host config, but then renders
`setup.config_path` by calling `default_host_setup_config_path()` again outside
that guarded path. If home/config path resolution is unavailable, the second
call escapes the MCP tool instead of returning structured source-checkout
status.

Scope is limited to guarding setup-status config path rendering on the explicit
source checkout fallback path.

### Requirements Checklist

- Preserve normal setup-status payloads with `setup.config_path` when the host
  setup config path is available.
- Preserve explicit `source_checkout` fallback behavior when host config
  resolution/read fails.
- Omit `setup.config_path` when the fallback path cannot safely resolve it
  instead of re-raising.
- Add a focused regression for explicit source checkout status when both the
  config read and default config path rendering fail.

### Implementation Steps

1. Add the focused failing regression for the guarded config path rendering
   fallback.
2. Carry an optional guarded config path value through `_get_setup_status_result`
   and only include `setup.config_path` when present.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_returns_only_status_and_safe_refs tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: Coverage Shard 8 Maintainability Guard

### Problem Statement And Scope

GitHub Actions run `26985743806` failed in `python-coverage-shards (8)` on
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
The guard reported `tests/unit/mcp/test_setup_tools.py` at 1525 lines, above
the 1500-line first-party file limit.

Scope is limited to restructuring the owned MCP setup tests so every first-party
test file stays below the existing maintainability limit. The guard itself is
not changed, skipped, or weakened.

### Requirements Checklist

- Keep all existing MCP setup/start/init/client behavior assertions intact.
- Move a coherent group of tests from `tests/unit/mcp/test_setup_tools.py` into
  an owned sibling test module.
- Keep each touched first-party test file below 1500 lines.
- Reproduce and then pass the focused maintainability guard locally.
- Run focused MCP setup test coverage for the touched test modules only.

### Implementation Steps

1. Reproduce the maintainability failure with the single guard test.
2. Move project-profile initialization MCP tests into a new
   `tests/unit/mcp/test_setup_tools_project_profile.py` module with only the
   imports those tests need.
3. Re-run the focused maintainability guard and the touched MCP setup test
   modules.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HOFU3

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` can return coherent
`source_checkout` metadata for the explicit checkout being probed while leaving
an upstream `awf start --source-checkout ...` next-step command pointed at a
different checkout.

Scope is limited to setup-status next-step rewriting when an explicit
`source_checkout` is supplied. The fix must continue to avoid duplicate
`--source-checkout` flags.

### Requirements Checklist

- Preserve bare `awf start` next-step rewriting for explicit checkout status
  probes.
- Rewrite existing `awf start --source-checkout ...` next-step commands to the
  explicit checkout being probed.
- Do not duplicate `--source-checkout` flags in returned next steps.
- Add a focused regression for an upstream `awf start --source-checkout ...`
  next step that names a different checkout.

### Implementation Steps

1. Update the focused setup-status next-step regression so it expects the
   explicit checkout path and still proves no duplicate flag is emitted.
2. Extend setup-status start-command rewriting to replace an existing
   source-checkout-aware start command with the normalized explicit checkout
   command.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Bootstrap Execution Failure Reason Code

### Problem Statement And Scope

The review reports that `_start_bootstrap_path_error_result()` handles
exceptions raised by `run_service_bootstrap()` but fabricates a
`START_INPUT_RESOLUTION_FAILED` diagnostic. That reason code belongs to the
earlier input-resolution phase and can mislead operators when Docker/bootstrap
execution fails after inputs were resolved.

Scope is limited to the MCP `awf_start_local_service` bootstrap exception
boundary and its focused regressions. Input-resolution failures should keep
their existing reason code and payload shape.

### Requirements Checklist

- Preserve `START_INPUT_RESOLUTION_FAILED` for failures raised while resolving
  start inputs before `run_service_bootstrap()` is called.
- Report `CalledProcessError`, `OSError`, `RuntimeError`, and `ValueError`
  raised from `run_service_bootstrap()` with a dedicated bootstrap-execution
  reason code.
- Keep bootstrap execution failure payloads structured as first-run `awf start`
  failures and continue excluding raw exception detail from MCP output.
- Add or update focused regressions for bootstrap-time `RuntimeError` and
  `CalledProcessError`.

### Implementation Steps

1. Update focused MCP start-service tests to expect the dedicated bootstrap
   execution reason code on bootstrap-time exceptions.
2. Confirm the updated regressions fail against the current implementation.
3. Add the dedicated reason code and message in
   `_start_bootstrap_path_error_result()`.
4. Run the targeted regressions and focused checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_called_process_error_is_structured -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_input_resolution_failure_is_structured tests/unit/mcp/test_setup_tools.py::test_start_local_service_runtime_input_resolution_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Command Suffix And Preview Probe Logging

### Problem Statement And Scope

The review reports two small diagnostics/command-guidance issues:

- setup-status source-checkout command rewriting does not match
  colon-terminated `awf setup --dry-run:` or `awf start:` next-step text;
- `_initialize_project_profile_result` catches existing-profile probing and
  onboarding preview failures in one broad `except Exception` block, so probe
  failures are logged with a preview-specific message.

Scope is limited to colon suffix matching and splitting the project-profile
probe and preview exception boundaries. External MCP payload shapes and
redaction behavior stay unchanged.

### Requirements Checklist

- Preserve existing setup-status rewrites for period, comma, semicolon,
  right-parenthesis, end-of-string, and `to` suffixes.
- Rewrite colon-terminated `awf setup --dry-run:` and `awf start:` next-step
  commands to include the explicit source checkout.
- Preserve the sanitized `PROJECT_INIT_FAILED` response for existing-profile
  probe failures and onboarding preview failures.
- Log existing-profile probe failures with a probe-specific message, while
  continuing to log preview failures with the preview-specific message.
- Add focused regressions for the colon rewrite and probe-specific logging.

### Implementation Steps

1. Add focused failing regressions for colon-terminated setup/start next-step
   rewrites and existing-profile probe logging.
2. Include `:` in the bounded command suffix regexes.
3. Split `_existing_project_profile_path()` and `preview_project_onboarding()`
   into separate exception boundaries with distinct log messages.
4. Run the targeted regressions and focused checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HM47u Bootstrap Preflight Path Expansion

### Problem Statement And Scope

The review reports that `awf_start_local_service` only converts
`ServiceBootstrapError` from `run_service_bootstrap()`. During the bootstrap
work-dir mount-propagation preflight, an invalid home-style path such as
`AWF_HOST_WORK_DIR=~nosuchuser/work` can make `Path.expanduser()` raise
`RuntimeError` before bootstrap reaches the structured stage-failure path.

Scope is limited to the MCP `awf_start_local_service` boundary around
`run_service_bootstrap()`. The review explicitly allows normalizing these
bootstrap-time path errors at this boundary, and the MCP tool must not let raw
bootstrap-time path errors escape.

### Requirements Checklist

- Preserve normal `ServiceBootstrapError` first-run failure handling.
- Treat an unexpandable work-dir path raised from `run_service_bootstrap()` as a
  structured `awf start` first-run failure instead of letting `RuntimeError`
  escape through FastMCP.
- Add focused MCP regression coverage for the unexpandable work-dir path case.

### Implementation Steps

1. Add a focused failing MCP regression that makes `run_service_bootstrap()`
   raise the same `RuntimeError` produced by an unexpandable `~user` path.
2. Add a narrow `run_service_bootstrap()` exception boundary for path-resolution
   exception types and render them through the existing first-run failure
   payload path.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## CI Repair: Setup Tools Test File Line Limit

### Problem Statement And Scope

CI fails the maintainability guard
`test_first_party_code_files_stay_under_line_limit` because
`tests/unit/mcp/test_setup_tools.py` grew to 1,721 lines, above the 1,500-line
first-party file limit.

Scope is limited to decomposing the oversized setup-tools test module while
preserving existing MCP setup-tool behavior and assertions.

### Requirements Checklist

- Keep every setup-tools test behavior and assertion intact.
- Split `tests/unit/mcp/test_setup_tools.py` so no first-party file exceeds
  the 1,500-line maintainability limit.
- Keep the existing coverage-registry node ID for the representative
  client-integration smoke test stable.
- Avoid weakening, skipping, or disabling the maintainability guard.
- Keep verification focused; broad AWF/GitHub validation remains managed after
  the agent phase.

### Implementation Steps

1. Move shared setup-tools test helpers into a small helper module.
2. Move client-integration instruction tests into a dedicated test module while
   leaving the registered smoke-test node in the original file.
3. Run the focused line-limit repro, affected MCP setup-tools tests, and the
   registry reference smoke test.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py -q
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_registry_smoke.py::test_mcp_implemented_matrix_rows_have_executable_coverage_reference -q
uv run --python 3.12 --extra dev ruff check tests/unit/mcp/test_setup_tools.py tests/unit/mcp/test_setup_tools_client_integration.py tests/unit/mcp/setup_tools_test_helpers.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HH2Ia

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` catches
only `OSError` while planning client instructions. Codex config planning can
raise `RuntimeError` from `Path.expanduser()` when `CODEX_HOME` contains an
invalid home override such as `~nosuchuser`, and similar invalid path/config
planning failures can raise `ValueError`. Those should return the same
structured, redacted client-config blocked payload as `OSError`.

Scope is limited to the client-instruction planning exception boundary.

### Requirements Checklist

- Preserve existing structured handling for `SetupCheckError`,
  `SourceCheckoutError`, and `OSError`.
- Convert client-instruction planning `RuntimeError` failures into a structured
  `CLIENT_CONFIG_CONFLICT` blocked MCP result without exposing raw exception
  text.
- Convert client-instruction planning `ValueError` failures into the same
  structured blocked result.
- Add focused regressions covering the newly handled exception types.

### Implementation Steps

1. Add focused failing MCP regressions for `RuntimeError` and `ValueError`
   during client instruction planning.
2. Extend the existing client-instruction planning catch block to include
   `RuntimeError` and `ValueError`.
3. Run the targeted regressions and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_codex_invalid_home_override_is_structured tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_planning_value_error_is_generic -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Next-Step Command Rewriting

### Problem Statement And Scope

The review reports that `_setup_status_next_steps` rewrites source-checkout
commands with blanket substring replacement. That can duplicate
`--source-checkout` flags when an upstream next-step already includes a
source-checkout-aware `awf start ...` command, or rewrite descriptive text that
only happens to contain the command substring.

Scope is limited to setup-status next-step command rewriting when an explicit
`source_checkout` is supplied. Existing source-checkout command strings and
response schemas stay unchanged.

### Requirements Checklist

- Preserve existing rewrites for known setup-status next-step command shapes:
  `awf setup --dry-run` and `awf start`.
- Do not rewrite `awf start` occurrences that already include trailing command
  arguments in the same shell token sequence.
- Do not duplicate `--source-checkout` flags in returned next steps.
- Add a focused regression for an upstream `awf start --source-checkout ...`
  next step.

### Implementation Steps

1. Add a focused failing MCP regression for an upstream source-checkout-aware
   `awf start` next step.
2. Replace blanket substring replacement with bounded command-pattern rewriting.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HHoLm

### Problem Statement And Scope

The PR review reports that `awf_get_client_integration_instructions` still
resolves source checkout and MCP env-file state when the caller explicitly
passes `clients: []`. That can turn a zero-client request into a blocked
client-config result because of stale persisted source checkout state, an
invalid explicit `source_checkout`, or compose env-file resolution failure.

Scope is limited to the explicit empty-client request path for client
integration instructions.

### Requirements Checklist

- Preserve omitted `clients` behavior, which still requests all supported
  clients.
- Preserve unknown-client validation for non-empty client lists.
- For explicit `clients: []`, return a successful empty client instruction
  payload before resolving source checkout or env-file state.
- Add a focused regression proving env-file resolution is skipped for explicit
  empty-client requests.

### Implementation Steps

1. Add a focused failing MCP regression where explicit `clients: []` would fail
   if `_resolve_client_env_file()` runs.
2. Normalize clients before resolving source checkout/env-file state and return
   the empty success payload immediately when the normalized selection is empty.
3. Run the targeted regression and focused checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_client_integration_instructions_preserves_explicit_empty_clients -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HHUuk

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` validates an explicit
`source_checkout` with `_run_setup(..., dry_run=True)` but returns the generic
first-run command and next-step guidance. Those strings can point the operator
at `awf start` or `awf setup --dry-run` without `--source-checkout`, so following
the MCP response may inspect or start persisted/default assets instead of the
explicit checkout that was just probed.

Scope is limited to setup-status response guidance for calls that include an
explicit `source_checkout`.

### Requirements Checklist

- Preserve existing setup-status command and next-step output when
  `source_checkout` is not supplied.
- For explicit `source_checkout` status calls, render a setup command containing
  the resolved checkout path.
- For successful explicit-checkout status calls, render next-step guidance that
  starts local service with the same resolved checkout path.
- For blocked explicit-checkout status calls, render next-step guidance that
  re-runs setup dry-run with the same resolved checkout path.
- Add focused regressions proving the returned guidance preserves the resolved
  explicit checkout path.

### Implementation Steps

1. Add focused failing setup-status regressions for success and blocked
   explicit-checkout next steps.
2. Add a small helper to render source-checkout-aware setup/status/start
   commands and use it only when `source_checkout` is supplied.
3. Run the targeted regressions and focused setup-tools checks.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_reads_host_config_status tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue_4620143523

### Problem Statement And Scope

The PR review reports that unguarded non-domain exceptions from `_run_setup`
inside `awf_get_setup_status` can propagate through FastMCP without first
passing through the shared safe-result redaction path. Plausible first-run
failures include filesystem, subprocess, or runtime errors that may contain
local paths or token-like details in their exception strings.

Scope is limited to the setup-status `_run_setup` exception boundary and a
focused regression proving the MCP result stays structured and redacted.

### Requirements Checklist

- Preserve the explicit `SetupCheckError` and `HostSetupConfigError` structured
  reason-code payloads.
- Convert generic setup-status probe failures into a redacted first-run blocked
  result instead of allowing the raw exception to escape.
- Expose only the exception type for generic probe failures; do not echo the
  exception message or local path details.
- Add a focused regression for an `_run_setup` `OSError` containing a
  token-like value and path.

### Implementation Steps

1. Add the focused failing MCP regression for a generic `_run_setup` failure.
2. Add a fallback exception handler in `_get_setup_status_result` that returns a
   `SETUP_READINESS_FAILED` first-run payload with only `error_type` details.
3. Run the targeted regression and focused setup-tools checks for the touched
   behavior.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_host_config_error_without_source_checkout_is_structured tests/unit/mcp/test_setup_tools.py::test_get_setup_status_run_setup_oserror_is_structured_and_redacted -q
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6HDuez

### Problem Statement And Scope

The PR review reports that `_resolve_user_supplied_path()` catches
`OSError`/`RuntimeError` around `Path.resolve()` but lets `ValueError` escape
for malformed user path strings such as embedded NUL bytes. Because MCP setup
tools normalize `project_path` and `source_checkout` before their structured
error handling can run, malformed input can surface as a FastMCP tool exception
instead of a safe structured payload.

Scope is limited to guarded user-supplied path normalization and focused MCP
regressions for malformed init/setup/start path inputs.

### Requirements Checklist

- Preserve normal resolved-path behavior for valid absolute and relative paths.
- Treat `ValueError` from user path expansion/resolution like the existing
  guarded normalization failures.
- Return structured MCP errors for malformed `project_path` values passed to
  `awf_initialize_project_profile`.
- Preserve structured MCP behavior for malformed `source_checkout` values passed
  to setup/start tools.
- Keep response payloads secret-free and avoid broad validation in the agent
  phase.

### Implementation Steps

1. Add focused failing MCP regressions for embedded-NUL `project_path` and
   `source_checkout` inputs.
2. Include `ValueError` in `_resolve_user_supplied_path()` guarded fallbacks.
3. Run the targeted regressions plus focused lint/type checks for changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_path_value_error_returns_structured_error tests/unit/mcp/test_setup_tools.py::test_get_setup_status_source_checkout_value_error_uses_guarded_fallback tests/unit/mcp/test_setup_tools.py::test_start_local_service_source_checkout_value_error_is_structured -q
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

## Review Repair: issue:4620143523 Preview Failure Logging

### Problem Statement And Scope

The PR review reports that `_initialize_project_profile_result` catches broad
preview/probe failures and returns a sanitized `PROJECT_INIT_FAILED` MCP
response without logging the underlying exception. Operators then cannot
diagnose malformed templates, import failures, serialization errors, or probe
failures that trigger the safe response path.

Scope is limited to adding structured exception logging for the existing
preview/probe failure boundary while preserving the redacted MCP response.

### Requirements Checklist

- Preserve the existing `PROJECT_INIT_FAILED` MCP response and redaction
  behavior.
- Record the caught preview/probe exception with exception context before
  returning the sanitized result.
- Include safe operational context in the log entry: project path and template.
- Add a focused regression proving the preview/probe failure path emits an
  exception log while keeping raw exception text out of the MCP response.

### Implementation Steps

1. Extend the focused preview-failure regression to assert an exception log is
   emitted.
2. Add module-level logging and call `logger.exception(...)` inside the existing
   preview/probe `except Exception` clause.
3. Run the targeted regression and focused checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_preview_failure_does_not_surface_exception_text tests/unit/mcp/test_setup_tools.py::test_initialize_project_profile_existing_profile_probe_failure_is_structured -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: PRRT_kwDOSJAM6s6Hc4Vi Preserve Start Source-Checkout Setup Remediation

### Problem Statement And Scope

The review reports that `awf_start_local_service` source-checkout validation
failures currently rewrite the source-checkout catalog remediation from
`awf setup --source-checkout .` to the same failing `awf start --source-checkout
...` command. That loses the setup recovery path operators need to verify and
refresh source-checkout metadata.

Scope is limited to start-tool issue remediation rewriting for
SOURCE_CHECKOUT_INVALID / SOURCE_CHECKOUT_ASSETS_STALE issues. The top-level
start command, ordinary start failure remediation rewrites, and the
START_COMPOSE_ASSETS_MISSING no-source-checkout exception stay unchanged.

### Requirements Checklist

- Preserve the top-level explicit `awf start --source-checkout ...` command on
  source-checkout validation failures.
- Preserve the setup recovery path in
  `issues[].remediation.related_command` for source-checkout validation
  failures, using the resolved explicit checkout path when available.
- Continue rewriting ordinary start remediation commands to the rendered start
  command.
- Preserve the existing START_COMPOSE_ASSETS_MISSING behavior when no explicit
  `source_checkout` is supplied.
- Update the focused regression for the structured issue remediation command.

### Implementation Steps

1. Update the explicit source-checkout validation-failure regression to expect
   the setup recovery remediation and confirm it fails before implementation.
2. Change the start issue remediation rewrite helper so source-checkout setup
   remediations rewrite to `awf setup --source-checkout <path>` instead of the
   failing start command.
3. Run the targeted regression, adjacent focused remediation tests, and focused
   lint/type checks for the changed files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_explicit_source_checkout_validation_failure_command -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command tests/unit/mcp/test_setup_tools.py::test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
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

## Review Repair: PRRT_kwDOSJAM6s6HPk73

### Problem Statement And Scope

The PR review reports that `awf_get_setup_status` returns
`_reason_coded_payload(...)` unchanged when `_run_setup` raises
`SetupCheckError` before normal setup-status payload transformation, such as an
unsupported provider selector. That error payload renders the generic
`awf setup` command, so an MCP operator copying it can run the mutating setup
flow instead of the matching read-only `awf setup --dry-run ...` status check.
It also drops the explicit `source_checkout` context from the retry command.

Scope is limited to the setup-status `SetupCheckError` branch and a focused
regression for the returned command.

### Requirements Checklist

- Preserve the existing reason-coded setup-status error payload shape,
  redaction, reason code, issue details, and MCP error behavior.
- Render the setup-status `SetupCheckError` command as
  `awf setup --dry-run` with the original provider selectors.
- Preserve explicit `source_checkout` in that dry-run retry command.
- Keep setup-status provider-selector guidance read-only by rendering
  `awf setup --dry-run` in the top-level next step.
- Add a focused regression proving the early error path returns the matching
  dry-run status command.

### Implementation Steps

1. Add the focused failing MCP regression for setup-status `SetupCheckError`
   command rendering.
2. Wrap the existing reason-coded payload in the setup-status command helper
   before returning it from the `SetupCheckError` branch.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools.py::test_get_setup_status_setup_check_error_returns_matching_dry_run_command -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.

## Review Repair: issue:4620143523 Probe Failure Message

### Problem Statement And Scope

The review reports that `_initialize_project_profile_result` logs an existing
profile probe failure correctly, but returns the same sanitized MCP message as
the later onboarding-preview generation failure. That obscures which stage
failed for MCP callers.

Scope is limited to the existing-profile probe exception branch in
`awf_initialize_project_profile` and its focused regression. The error code,
redacted detail payload, logging context, and preview-generation error message
remain unchanged.

### Requirements Checklist

- Preserve the structured `PROJECT_INIT_FAILED` response and safe redaction.
- Return a stage-specific sanitized message when the existing profile probe
  fails.
- Keep the preview-generation failure message unchanged.
- Update the existing focused regression for the probe-failure branch.

### Implementation Steps

1. Update the existing probe-failure regression to expect the stage-specific
   sanitized message and confirm it fails before the implementation change.
2. Change only the probe-failure `_error_result(...)` message in
   `src/awf/mcp/setup_tools.py`.
3. Run the targeted regression and focused lint/type checks for the changed
   files.

### Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_setup_tools_project_profile.py::test_initialize_project_profile_existing_profile_probe_failure_logs_probe_context -q
uv run --python 3.12 --extra dev ruff check src/awf/mcp/setup_tools.py tests/unit/mcp/test_setup_tools_project_profile.py
uv run --python 3.12 --extra dev mypy src/awf/mcp/setup_tools.py
```

Full AWF/GitHub validation and coverage gates remain managed by AWF after the
agent phase.
