# Launch-Time LLM Provider Readiness Preflight Plan

## Goal

Add a strict launch-time preflight for workspace create and retry that checks the selected agent/model provider before AWF admits provisioning. The preflight must report the selected provider, model, readiness status, auth source, probe status, override state, and reason codes through REST, CLI, MCP, console, and workspace events while preserving existing create/retry semantics for successful launches and idempotent replays.

## Current Shape

- `src/awf/service/provider_readiness.py` already reports service-level credential readiness for `github`, `codex`, `claude_code`, `gemini`, `opencode`, and `docker`, with strict provider handling and an OpenCode/Ollama HTTP probe.
- `src/awf/service/workspaces.py` owns shared v2 create and retry row creation. REST and MCP both call this layer for v2 create/retry behavior.
- `src/awf/api/routes/workspaces.py` performs v2 admission disk checks before creating rows, and retry currently creates a fresh row without provider preflight.
- Effective agent/model defaults already live in `src/awf/adapters/defaults.py` and are projected through workspace observability.
- Workspace events are append-only JSON payloads via `WorkspaceRepository.add_event`, so preflight pass/override events can be emitted without schema migration.

## Intended Files And Modules To Touch

- `src/awf/service/provider_readiness.py`
  - Add a selected-launch preflight abstraction that maps `AgentRuntime` + effective model to readiness provider names.
  - Extend Claude Code and Gemini readiness beyond file/env presence with real non-secret probes where possible.
  - Keep probes injectable for tests and never return secret values.
- `src/awf/service/workspaces.py`
  - Call the selected-provider preflight from `create_workspace_v2_row` and `retry_workspace_row` before row creation/admission.
  - Persist preflight snapshots in `task_policy` and emit workspace events for pass/override paths.
  - Add typed exceptions for blocked preflight failures.
- `src/awf/api/schemas.py`
  - Add request fields for explicit override, likely under v2 task or a small launch/preflight object.
  - Add response models for preflight status and include it in `WorkspaceAcceptedResponse`, `WorkspaceRetryResponse`, `WorkspaceResponse`, and overview responses if needed for console parity.
- `src/awf/api/routes/workspaces.py`
  - Return structured 409 or 422 style errors when readiness blocks launch.
  - Ensure idempotent replay returns the original accepted response and does not rerun/block on new local readiness.
- `src/awf/cli/main.py`
  - Add `awf workspace create --provider-readiness-override` and matching retry override option.
  - Surface blocked preflight payloads in JSON and readable pretty output through existing response handling.
- `src/awf/mcp/server.py`
  - Add override arguments to `awf_create_workspace_v2` and retry tool surface.
  - Return structured MCP errors for blocked preflight failures.
- `apps/console/lib/types.ts`
  - Add provider preflight/readiness types.
- `apps/console/components/console-dashboard.tsx`
  - Show selected provider/model readiness status in workspace detail and timeline-adjacent context.
  - Surface retry/create blocked errors with provider/model/auth-source detail where the console calls retry.
- `apps/console/app/api/awf/[...path]/route.ts`
  - Only if v2 create proxy routing is needed for console parity; otherwise leave unchanged.
- `TODO/pre-gke-industrial-readiness.md`
  - Mark the backlog item complete only after code, tests, and console/API/MCP/CLI surfaces satisfy acceptance.

## Tests To Write First

1. `tests/unit/service/test_provider_readiness.py`
   - Selected preflight maps `codex`, `claude_code`, `gemini`, and `opencode` to provider readiness entries and effective models.
   - Missing strict auth produces a blocking preflight result with provider, model, auth-source status, readiness status, and reason code.
   - Override changes the result to admitted-with-override but preserves the original failing reason.
   - Claude Code OAuth file/env readiness requires an injected non-secret CLI probe; stale/unusable OAuth fails even when files exist.
   - Gemini file/env readiness requires an injected non-secret CLI/API probe; stale/unusable auth fails even when files exist.
   - Probe failures are redacted and do not leak tokens, config file contents, or raw credential paths beyond existing signal labels.

2. `tests/unit/service/test_workspaces.py` or `tests/unit/service/test_workspace_retry.py`
   - `create_workspace_v2_row` blocks before workspace/resource/task rows are created when selected provider readiness is missing and no override is set.
   - With override, create succeeds and the workspace task policy includes a redacted preflight snapshot.
   - Retry blocks before creating a new attempt when the retried workspace selected provider/model is not ready.
   - Retry with override creates the retry attempt and records source workspace, target provider, target model, override reason, and preflight snapshot.
   - Successful preflight emits a `workspace.provider_readiness_preflight` event; override emits a distinct override reason.

3. `tests/unit/api/test_workspaces.py` and `tests/unit/api/test_workspace_retry.py`
   - `POST /v2/workspaces` returns a structured blocking error when readiness fails, including exact provider/model/readiness/auth-source fields.
   - `POST /v2/workspaces` with override returns 202 and includes the preflight summary.
   - Idempotency replay returns the original workspace without rerunning preflight.
   - `GET /v1/workspaces/{id}`, `GET /v1/workspaces`, and overview include the stored preflight summary.
   - `POST /v1/workspaces/{id}/retry` returns structured blocking errors and supports override.

4. `tests/unit/mcp/test_mcp_server.py`
   - `awf_create_workspace_v2` exposes override input schema.
   - MCP create returns structured error content on blocked preflight.
   - MCP create/retry with override returns workspace/retry payload with preflight summary.

5. `tests/unit/cli/test_cli.py`
   - `awf workspace create` passes the override flag in the v2 request body.
   - `awf workspace retry` passes the override query/body field and prints structured blocked errors.

6. Console tests under `apps/console`
   - Update type fixtures in existing Playwright tests to include provider preflight.
   - Add a focused component/format test if an extracted formatter is used, proving blocked/override/pass statuses render without overflowing existing panels.

## Implementation Approach

1. Define a small public preflight shape:
   - `provider`, `agent`, `model`, `readiness_status`, `auth_status`, `auth_source`, `probe_status`, `reason_code`, `message`, `override_required`, `override_used`, `blocks_launch`, `checked_at`.
   - Keep `credential_sources` redacted and consistent with existing provider readiness diagnostics.

2. Add selected-provider preflight helpers:
   - Use `effective_agent_identity` or the same central defaults to derive the model before launch.
   - Map `codex -> codex`, `claude_code -> claude_code`, `gemini -> gemini`, `opencode -> opencode`.
   - Reuse `collect_agent_readiness(..., strict_providers={selected_provider})` for common status, then add selected model and provider probe details.
   - Keep `github` and `docker` out of this LLM preflight unless later policy requires them.

3. Add real non-secret probes:
   - Claude Code: injectable subprocess probe such as a CLI auth/status/version command that validates OAuth usability without sending prompt content or exposing token material.
   - Gemini: injectable subprocess or HTTP probe that validates CLI/auth account/model accessibility without prompt content.
   - OpenCode/Ollama already has `/api/version`; extend or add model availability probe if current model-specific readiness is missing.
   - Codex can initially use file/env/API-key readiness unless there is an existing cheap official CLI status path in the installed runtime.

4. Wire create and retry:
   - Run preflight after request validation, idempotency replay, disk admission, and profile resolution inputs are known, but before DB row creation and resource reservation.
   - On block, raise a typed `WorkspaceProviderReadinessBlockedError` carrying the redacted preflight payload.
   - On override, proceed and store the preflight summary in `task_policy["provider_readiness_preflight"]`.
   - Emit events after row creation for passed/overridden preflight. Blocked failures have no workspace row, so they are reported in API/CLI/MCP error payloads rather than workspace events.

5. Public surfaces:
   - REST returns a stable error code such as `PROVIDER_READINESS_PRECHECK_FAILED` with a `detail.provider_readiness_preflight` object.
   - REST accepted/retry responses include the preflight summary when a row is created.
   - CLI and MCP add explicit override options and do not hide provider failure details.
   - Console displays persisted preflight summary in the Workspace panel or Secrets & Leases area and shows retry blocked details in the existing retry error callout.

6. TODO update:
   - Only mark the `Launch-time LLM provider readiness preflight` item checked after all touched surfaces are covered by tests.

## Validation Commands

Focused Python tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness.py tests/unit/service/test_workspaces.py tests/unit/service/test_workspace_retry.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_workspace_retry.py -q
uv run --python 3.12 --extra dev pytest tests/unit/mcp/test_mcp_server.py tests/unit/cli/test_cli.py -q
```

Focused console checks:

```bash
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
```

Broader required surface:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

If console files are touched beyond type-only changes:

```bash
npm --prefix apps/console run build
```

## Risks

- Real provider probes can be slow or flaky. They need short timeouts, injectable test doubles, deterministic reason codes, and no retries that hide the original failure.
- Some provider CLIs may not expose a perfect non-secret account/model probe. The implementation should document the exact command/probe used and return `probe_status="unavailable"` only when a provider lacks a safe probe, not when auth is stale.
- Blocking before row creation means blocked launches cannot have workspace events. REST/CLI/MCP error payloads must carry equivalent detail; only pass/override paths can create workspace events.
- Adding request fields can affect idempotency comparison. Existing idempotency behavior must account for override/preflight policy deterministically.
- Console type additions can reveal missing fixture fields in existing tests; update fixtures narrowly.

## Assumptions

- This slice applies to v2 workspace creation as the primary create path; legacy `/v1/workspaces` is preserved unless acceptance tests reveal it is still considered a launch surface.
- The selected LLM provider is derived from `task.agent` and effective model, not from GitHub/Docker provider readiness.
- Explicit override is a request-time boolean with an optional reason string; override is audited in task policy and events.
- No database migration is needed because preflight snapshots can live in existing JSON `task_policy` and event payloads.
- Provider probes are allowed to contact provider-local or provider-owned endpoints but must not send task prompt content or log secret values.

## Explicit Non-Goals

- Do not redesign provider fallback/recovery policy or circuit breakers.
- Do not add cloud secret broker behavior.
- Do not make owned-path overlap warnings block launch.
- Do not broaden this into full release-readiness scorecard changes beyond reusing provider readiness helpers.
- Do not mark the TODO backlog item complete until implementation and validation are actually done.
