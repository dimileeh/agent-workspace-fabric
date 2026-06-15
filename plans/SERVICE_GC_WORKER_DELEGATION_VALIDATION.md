# Service GC Worker-Delegation Validation

Plan reference: `SERVICE_GC_WORKER_DELEGATION_PLAN.md`

This validation records the provenance for the on-demand GC worker-delegation
feature (#582, PR #590). Per `plans/PLAN_EXECUTION_PROTOCOL.md`, status is given
requirement-by-requirement with evidence (files + tests). Broad AWF/CI gates
(aggregate 99% coverage, full validation suite) run after the agent phase and
are not re-executed here.

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | API→worker channel table + repo | Complete | `migrations/versions/b582d1c4e7a9_service_gc_requests.py`, `src/awf/db/models.py` (`ServiceGCRequest`), `src/awf/db/repositories/service_gc_request_repo.py`; tests `tests/unit/db/test_service_gc_request_repository.py`, `tests/unit/db/test_migration_graph.py` |
| 2 | API-side reclaim then delegate capability-gated reap | Complete | `src/awf/service/gc_request.py`, `gc_worker_delegation.py`, `gc_worker_trigger.py`; tests `tests/unit/service/test_gc_worker_delegation.py`, `test_gc_worker_trigger.py` |
| 3 | Fold worker bytes/paths into response, no double count / no worker-only loss | Complete | `src/awf/service/gc_request.py`, `gc_terminal_passes.py`, `gc_claude_base.py`, `src/awf/control/worker/cleanup_service_gc.py`; tests `tests/unit/service/test_worker_terminal_gc_reaper.py`, `test_gc_claude_base.py`, `tests/unit/api/test_service_gc_worker_delegation.py` |
| 4 | Dry-run mirrors execute (preview pass, no trigger/delete) | Complete | `src/awf/service/gc_request.py` (docstring + second-pass preview), `gc_terminal_passes.py`; tests `tests/unit/api/test_service_gc.py` |
| 5 | Operator filters honored by both passes | Complete | `src/awf/cli/service_commands.py` (`gc` command), `src/awf/service/gc_request.py`, `gc_worker_trigger.py`; tests `tests/unit/cli/test_service_gc_cli.py`, `tests/unit/service/test_gc_worker_trigger.py` |
| 6 | Timeout/deadline budgeting across both phases + poll past deadline = timeout | Complete | `src/awf/cli/service_commands.py` (`2*timeout_seconds+30` budget), `src/awf/service/gc_request.py`, `gc_worker_delegation.py`; tests `tests/unit/service/test_gc_worker_delegation.py`, `tests/unit/api/test_service_gc.py` |
| 7 | Terminal-status bookkeeping (no overwrite of expired; recover stuck; param-parse = terminal) | Complete | `src/awf/db/repositories/service_gc_request_repo.py`, `src/awf/control/worker/cleanup_service_gc.py`; tests `tests/unit/db/test_service_gc_request_repository.py`, `tests/unit/control/test_worker_parts/test_worker_part_service_gc_trigger.py` |
| 8 | Reason codes / OpenAPI drift / CLI↔server vocabulary | Complete | `src/awf/api/routes/service.py`, `src/awf/api/schemas.py`, `src/awf/api/schemas_responses.py`, `openapi.json`; tests `tests/unit/api/test_service_gc.py`, `tests/unit/cli/test_service_gc_cli.py` (reason-code drift guard) |

## Evidence — commands

Targeted suites for the touched surface (run during the agent phase):

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_worker_delegation.py \
  tests/unit/service/test_gc_worker_trigger.py \
  tests/unit/service/test_gc_claude_base.py \
  tests/unit/api/test_service_gc.py \
  tests/unit/api/test_service_gc_worker_delegation.py \
  tests/unit/cli/test_service_gc_cli.py \
  tests/unit/db/test_service_gc_request_repository.py -q
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

## Gaps / iteration

No `Partial` or `Missing` requirements. The aggregate 99%-coverage gate and the
full `.awf/workspace.yml` validation suite are owned and executed by AWF/CI
after the agent phase; this doc records the focused provenance only.
