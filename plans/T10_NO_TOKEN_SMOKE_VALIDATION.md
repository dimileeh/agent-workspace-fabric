# T10 — No-Token Local Proof and Mocked Smoke Path (VALIDATION)

Plan reference: `plans/T10_NO_TOKEN_SMOKE_PLAN.md`
Implementation contract: `docs/awf-plans/ws_bcb857fd33ea4c1dbd4d3962.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
| - | ----------- | ------ | -------- |
| 1 | Remove `mocked_local` warn-downgrade; unhealthy Core always `fail` | Complete | `_phase_service_readiness` in `src/awf/service/smoke.py` no longer takes/uses `mocked_local`; unreachable ⇒ `fail`. Test `test_service_readiness_fails_in_mocked_with_unreachable_status` asserts phase `fail` + overall `fail`. |
| 2 | Richer collector result (`api`/`worker`) + enriched evidence; backward compatible | Complete | Phase reads `api`/`worker`; evidence carries both. `test_service_readiness_ok_in_mocked_exposes_api_and_worker_evidence` and `test_service_readiness_plain_ok_collector_stays_ok`. |
| 3 | New reason code `SMOKE_WORKER_UNAVAILABLE` | Complete | Emitted when API up but worker substrate down. `test_service_readiness_fails_when_worker_substrate_down_in_mocked`. No catalog entry needed (catalog test scans `error_code`, not `reason_code`) — confirmed by `tests/unit/docs/test_catalog_coverage.py` green. |
| 4 | `_default_service_collector` probes `/healthz` + `/readyz`, reads `checks.db.ok` | Complete | New `_probe_worker_substrate` parses `/readyz` JSON regardless of status; degrades gracefully. Tests: `..._returns_ok_when_db_ready`, `..._worker_fail_when_db_down_even_on_503`, `..._returns_unreachable_on_503_healthz`, `..._worker_fail_when_readyz_raises`. |
| 5 | `awf start` next steps lead with provider-free proof | Complete | `_start_success_payload` in `src/awf/cli/start_commands.py`. Tests: `test_start_success_payload_leads_with_provider_free_proof`, `test_start_success_json_payload`. |
| 6 | `smoke run` help describes no-token local proof | Complete | `_DX_HELP` + `--mocked-local` help in `src/awf/cli/profile_smoke_commands.py`. Test: `test_help_describes_provider_free_local_proof`. |
| 7 | setup→start→smoke chain stays provider-free | Complete | `test_setup_success_next_step_is_provider_free_and_leads_to_start` (setup next step already provider-free via `_readiness_next_steps`). |
| 8 | Console false-green coverage preserved | Complete | `test_console_unavailable_reports_reason_code` / `test_configured_console_url_reports_unavailable_when_probe_fails` unchanged and green. |

## Files changed

- `src/awf/service/smoke.py` — hard local-Core health; `SMOKE_WORKER_UNAVAILABLE`;
  dual-probe `_default_service_collector` + `_probe_worker_substrate`.
- `src/awf/cli/start_commands.py` — provider-free leading next step.
- `src/awf/cli/profile_smoke_commands.py` — no-token help text.
- `tests/unit/service/test_smoke_parts/test_smoke_part_002.py` — regression flip +
  new phase/collector tests.
- `tests/unit/cli/test_start_commands.py`, `test_smoke.py`, `test_setup_commands.py`
  — first-run-chain assertions.

## Commands run (focused; broad gate owned by AWF after agent completion)

- `ruff check src/awf tests` → All checks passed.
- `ruff format --check src/awf tests` → already formatted.
- `mypy src/awf` → Success: no issues found in 326 source files.
- `pytest tests/unit/service/test_smoke_parts tests/unit/cli/test_smoke.py
  tests/unit/cli/test_start_commands.py tests/unit/cli/test_setup_commands.py
  tests/unit/docs/test_catalog_coverage.py` → 164 passed.
- `pytest tests/unit/cli/test_profile_smoke_commands_edges.py
  tests/unit/cli/test_clean_install_smoke.py` → 9 passed.

Broad coverage/CI validation (full suite + 99% gate) is owned by AWF/GitHub after
agent completion.
