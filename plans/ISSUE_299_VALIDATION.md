# Issue #299 — Make `SERVICE_STARTUP_FAILURE` self-diagnostic (VALIDATION)

Plan reference: `plans/ISSUE_299_PLAN.md` (and the saved AWF plan
`docs/awf-plans/ws_4f34d09edec147339a6703f5.md`).

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | Capture last-N companion `docker logs` (N configurable, default 200) | Complete | `ComposeManager.capture_companion_diagnostics` → `companion_logs[<svc>]` (list of lines); `tail_lines` param; default `DEFAULT_SERVICE_STARTUP_LOG_TAIL_LINES = 200`; configurable via `AWF_WORKER_SERVICE_STARTUP_LOG_TAIL_LINES`. Tests: `test_happy_path_*`, `test_default_tail_lines_used_when_unspecified`. |
| 2 | Capture `.State.Health.Log` (ExitCode + Output) per unhealthy companion | Complete | `_container_health_summary` → `companion_health[<svc>].health_log` with `ExitCode`/`Output`. Test: `test_happy_path_*` asserts `health_log[0]["ExitCode"] == 1`. |
| 3 | Capture rendered healthcheck `test` array | Complete | `_container_healthcheck_test` → `healthcheck_test[<svc>]`. Test: `test_happy_path_*`. |
| 4 | Include rendered `compose.yml` path + project + tail_lines + count | Complete | `compose_file`, `compose_project`, `tail_lines`, `containers_inspected` in payload. |
| 5 | `payload` no longer `None` on `SERVICE_STARTUP_FAILURE` | Complete | Provisioner threads `event_payload` into `repo.transition(..., payload=...)`. Test: `test_failure_event_payload_carries_captured_diagnostics`. |
| 6 | Redact everything persisted (`redact_audit_value`) / logged (`redact_secrets`) | Complete | Final `redact_audit_value(payload)`; every `_log.*` content line via `redact_secrets`. Tests: `test_redacts_planted_secrets_*`, `test_end_to_end_real_capturer_redacts_planted_secret`. |
| 7 | Best-effort; never masks original error; reason_code preserved | Complete | Capturer never raises; provisioner re-raises original `ComposeOperationError`. Test: `test_capture_failure_does_not_mask_original_error` asserts `reason_code == "COMPOSE_COMMAND_FAILED"`. |
| 8 | Capture errors degrade via `companion_logs_capture_error` marker | Complete | Per-container + top-level markers. Tests: `test_per_container_logs_error_*`, `test_top_level_ps_error_*`, `test_inspect_error_*`, `test_unparseable_inspect_json_*`. |
| 9 | Capture happens BEFORE `_mark_failed`/teardown | Complete | Capture call is first in the `except` block. Test: `test_capture_runs_before_mark_failed_teardown` asserts order `["capture", "mark_failed"]`. |
| 10 | Catch specific exceptions, not bare `Exception` | Complete | Only `ComposeOperationError` / `json.JSONDecodeError` caught; defensive `isinstance` guards for malformed inspect JSON (no bare except). |
| 11 | No new docker-exec abstraction — reuse `_docker_capture`/`_docker_resource_ids` | Complete | Both reused; `_docker_capture` gained an optional `combine_stderr` flag (see Assumptions). |
| 12 | No hard-coded project-specific service names | Complete | Generic over `com.docker.compose.service` labels with an Id fallback. Test: `test_exited_container_without_healthcheck_keyed_by_id_fallback`. |
| 13 | Regression test: planted secret absent from persisted payload | Complete | `test_redacts_planted_secrets_in_logs_and_health_output` (URL cred, `KEY=value`, `Bearer`, provider token) + e2e `test_end_to_end_real_capturer_redacts_planted_secret`. |
| 14 | 99% line+branch coverage on new code | Complete | `compose_manager.py` 99.55% (remaining 2 partials are pre-existing `_compose`/retry branches); new `provisioner` lines covered by the new tests; the only new uncovered line is the `Protocol` `...` stub (same pattern as the existing `WorkspaceStackLauncher` Protocol, tolerated under the whole-repo gate). |

All requirements: **Complete**. No `Partial`/`Missing` items, so no iteration section is required.

## Files changed

Source:
- `src/awf/node/compose_manager.py` — `capture_companion_diagnostics` (+ helpers, constants);
  `_docker_capture(..., combine_stderr=...)`.
- `src/awf/node/provisioner.py` — `ServiceStartupDiagnosticsCapturer` Protocol;
  `service_diagnostics` dependency; `ProvisionerConfig.service_startup_log_tail_lines`;
  `_capture_service_startup_diagnostics`; capture-before-teardown wiring in the
  `except ComposeOperationError` block; `_mark_failed(event_payload=...)`.
- `src/awf/service/worker.py` — passes `service_diagnostics=compose` and the tail-lines config.
- `src/awf/service/config.py` — `ServiceSettings.service_startup_log_tail_lines` + mapping.
- `src/awf/common/config.py` — `worker_service_startup_log_tail_lines` setting.

Tests:
- `tests/unit/node/test_compose_manager_subprocess.py` — `TestCaptureCompanionDiagnostics` (13 cases).
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_003.py` — `TestServiceStartupDiagnostics` (5 cases).
- `tests/unit/service/test_worker.py` — wiring assertion + fake signature updates.
- `tests/unit/common/test_common_polish.py` — settings default/env/validation.
- `tests/unit/service/test_config_parts/test_config_part_001.py` — settings→ServiceSettings flow.

## Commands run (focused; per AWF agent-phase contract)

```
ruff check <touched files>                                  # All checks passed
ruff format --check <touched files>                         # formatted
mypy src/awf/node/compose_manager.py src/awf/node/provisioner.py \
     src/awf/service/worker.py src/awf/service/config.py \
     src/awf/common/config.py                               # Success: no issues
pytest tests/unit/node/ -q                                  # 333 passed
pytest <touched test files> -q                              # 220 passed (+ added cases)
pytest --cov=awf.node.compose_manager --cov=awf.node.provisioner (focused)  # compose 99.55%
```

Full-repo pytest, the 99% combined coverage gate, full `src/` mypy, and the OpenAPI drift gate
are owned by AWF/GitHub CI after the agent phase. No API/OpenAPI/CLI/migration/reason-code
changes were made, so the drift gate is not applicable.

## Capture-then-teardown ordering (PR explanation)

On a companion healthcheck failure, `docker compose up --wait` exits non-zero and
`ComposeManager.up()` raises `ComposeOperationError(COMPOSE_COMMAND_FAILED)`. The
provisioner's `except ComposeOperationError` handler now, **in order**: (1) captures
redacted companion diagnostics while the failed containers still exist, (2) records the
egress audit, (3) calls `_mark_failed(event_payload=<diagnostics>)` which persists the
`SERVICE_STARTUP_FAILURE` event with the populated payload, then (4) re-raises the original
error unchanged. Compose `down` still runs later via GC / `WorkspaceCleaner` — the teardown
contract is unchanged; capture is purely additive and strictly precedes it.
