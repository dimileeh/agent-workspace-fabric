# Issue #299 — Make `SERVICE_STARTUP_FAILURE` self-diagnostic (PLAN)

## Problem statement and scope

When a companion service's healthcheck fails to go green within
`compose_up_timeout_seconds`, `docker compose up -d --wait` exits non-zero.
`ComposeManager.up()` raises `ComposeOperationError(reason_code="COMPOSE_COMMAND_FAILED")`,
which propagates through `ComposeStackLauncher.launch` and is handled in
`Provisioner._provision_claimed_workspace` (`except ComposeOperationError`).
That handler calls `_mark_failed(..., FailureReason.service_startup_failure, ...)`,
which writes a `workspace.state_changed` `WorkspaceEvent` whose `payload` is
**`None`** with `reason_code="SERVICE_STARTUP_FAILURE"`. The failed companion
container and its logs still exist at the moment we catch the error (teardown via
GC / `WorkspaceCleaner` happens later, not synchronously here), so this is the
capture window.

**Scope:** capture per-companion diagnostics (redacted) into the persisted event
payload BEFORE `_mark_failed`/teardown, so the failure is self-diagnostic.
Generic AWF-core debuggability feature — lands in `src/awf/node`, not a profile.

## Requirements checklist

- [ ] Capture last-N lines of `docker logs` per unhealthy companion (N configurable, default 200).
- [ ] Capture `.State.Health.Log` entries (ExitCode + Output) per unhealthy companion.
- [ ] Capture the rendered healthcheck `test` array per unhealthy companion.
- [ ] Include rendered `compose.yml` path + compose project + tail_lines + containers_inspected.
- [ ] `payload` is no longer `None` on the `SERVICE_STARTUP_FAILURE` path.
- [ ] Every persisted string passes through `redact_audit_value`; any live log line through `redact_secrets`.
- [ ] Capture is BEST-EFFORT: never masks the original `ComposeOperationError`; original
      `reason_code` ("COMPOSE_COMMAND_FAILED") is preserved and re-raised.
- [ ] Capture errors degrade gracefully via a `companion_logs_capture_error` marker.
- [ ] Capture happens BEFORE `_mark_failed` (and therefore before any later `down`).
- [ ] Catch specific docker/subprocess exceptions (`ComposeOperationError`, `json.JSONDecodeError`),
      never bare `Exception`.
- [ ] No new docker-exec abstraction — reuse `ComposeManager._docker_capture` / `_docker_resource_ids`.
- [ ] No hard-coded project-specific service names (generic over compose-project containers).
- [ ] Regression tests prove a planted secret does NOT appear in the persisted payload.
- [ ] 99% line+branch coverage on new code (capture-failure branches included).

## Implementation steps

1. `src/awf/node/compose_manager.py`
   - Add `DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES = 200`,
     `SERVICE_STARTUP_DIAGNOSTICS_SCHEMA = "service_startup_diagnostics.v1"`.
   - Add `async def capture_companion_diagnostics(*, project_name, workspace_id, tail_lines=...) -> dict`.
     Best-effort, returns an already-redacted payload. Enumerate project containers via
     `_docker_resource_ids(["ps","-aq","--filter",label=...])`; `_docker_capture(["inspect", *ids])`;
     for each unhealthy container capture health summary, healthcheck test array, and
     `_docker_capture(["logs","--tail",N,id], combine_stderr=True)` (combined so stderr-only
     services like postgres are captured). Per-container `logs` failures → marker, continue.
     Top-level `ps`/`inspect` failure or unparseable JSON → top-level marker, return partial.
     Final `return redact_audit_value(payload)`.
   - Add optional `combine_stderr` kwarg to `_docker_capture` (default False preserves behavior).
2. `src/awf/node/provisioner.py`
   - Add structural `ServiceStartupDiagnosticsCapturer(Protocol)`.
   - `Provisioner.__init__`: add `service_diagnostics: ... | None = None`.
   - `ProvisionerConfig`: add `service_startup_log_tail_lines: int = DEFAULT_...`.
   - Add `_capture_service_startup_diagnostics(workspace_id)` (returns None when no capturer;
     belt-and-suspenders `try/except ComposeOperationError` marker).
   - In `except ComposeOperationError`: capture FIRST, then egress audit, then
     `_mark_failed(..., event_payload=diagnostics)`, then re-raise unchanged.
   - `_mark_failed`: add `event_payload` param threaded into `repo.transition(..., payload=...)`.
3. `src/awf/service/worker.py`: pass `service_diagnostics=compose` and
   `service_startup_log_tail_lines=settings.service_startup_log_tail_lines`.
4. `src/awf/service/config.py`: add `service_startup_log_tail_lines: int = 200` and map from
   `settings.worker_service_startup_log_tail_lines`.
5. `src/awf/common/config.py`: add `worker_service_startup_log_tail_lines: int = Field(default=200, gt=0, ...)`.

## Verification commands and pass criteria

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/node/test_compose_manager_subprocess.py \
  tests/unit/node/test_compose_manager.py \
  tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py \
  tests/unit/service/test_config_parts/test_config_part_001.py \
  tests/unit/common/test_common_polish.py -q
uv run --python 3.12 --extra dev ruff check <touched files>
uv run --python 3.12 --extra dev ruff format --check <touched files>
uv run --python 3.12 --extra dev mypy <touched src files>
```

Pass criteria: all focused tests green; payload populated + redacted; original error
re-raised with `COMPOSE_COMMAND_FAILED`; capture runs before teardown; no lint/type errors.
Full-repo pytest, 99% coverage gate, and full mypy are owned by AWF/GitHub after the agent phase.

## Assumptions / Changes from the saved AWF plan

- The saved plan said to call `_docker_capture(["logs", ...])` directly. `_docker_capture`
  returns only the command's **stdout**, but `docker logs` sends a container's stderr to the
  CLI's stderr — and the default/primary AWF companion (postgres) logs entirely to stderr.
  Capturing stdout-only would yield empty logs for exactly the failing-companion case the
  issue is about. To keep the fix useful while still reusing `_docker_capture` (no new
  abstraction), `_docker_capture` gains an optional `combine_stderr` flag (default `False`,
  fully backward compatible) used only by the log-capture call.
