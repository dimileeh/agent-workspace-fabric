# Worker Heartbeat Readiness Validation

Plan reference: `plans/WORKER_HEARTBEAT_READINESS_PLAN.md`

## Requirement Status

- Complete: Added durable `worker_heartbeats` ORM model, repository, and Alembic migration.
- Complete: `ControlWorker` writes a heartbeat from `run_once()` and maintains a periodic heartbeat loop in `run_forever()`.
- Complete: `/readyz` exposes token-free `checks.worker` with `WORKER_HEARTBEAT_FRESH`, `WORKER_HEARTBEAT_MISSING`, `WORKER_HEARTBEAT_STALE`, and `WORKER_HEARTBEAT_UNAVAILABLE`.
- Complete: Smoke now consumes `/readyz` `checks.worker` instead of mapping `checks.db.ok`, and carries `worker` plus `worker_reason` evidence end to end.
- Complete: CLI pretty/JSON output and `docs/SMOKE_COMMAND.md` describe the worker heartbeat proof rather than the old DB-substrate proxy.
- Complete: Focused tests cover fresh, missing, stale, unavailable, worker write, smoke fail-closed, CLI evidence, and migration schema behavior.

## Evidence

Changed implementation files include:

- `src/awf/db/models.py`
- `src/awf/db/repositories/system_repo.py`
- `migrations/versions/f9a0b1c2d3e4_worker_heartbeats.py`
- `src/awf/service/worker_heartbeat.py`
- `src/awf/control/worker/manager.py`
- `src/awf/api/routes/health.py`
- `src/awf/service/smoke.py`
- `src/awf/cli/common.py`
- `src/awf/cli/profile_smoke_commands.py`
- `docs/SMOKE_COMMAND.md`

Focused validation run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/control/test_worker_stop.py tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py tests/unit/db/test_migration_graph.py -q
```

Result: `111 passed in 41.89s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/routes/health.py src/awf/service/smoke.py src/awf/service/worker_heartbeat.py src/awf/cli/common.py src/awf/cli/profile_smoke_commands.py src/awf/control/worker/manager.py src/awf/control/worker/config.py src/awf/db/models.py src/awf/db/repositories/system_repo.py src/awf/db/repositories/__init__.py migrations/versions/f9a0b1c2d3e4_worker_heartbeats.py tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/control/test_worker_stop.py tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py tests/unit/db/test_migration_graph.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/routes/health.py src/awf/service/smoke.py src/awf/service/worker_heartbeat.py src/awf/cli/common.py src/awf/cli/profile_smoke_commands.py src/awf/control/worker src/awf/db/repositories/system_repo.py
```

Result: `Success: no issues found in 29 source files`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/control/test_worker_stop.py tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py --cov=awf.api.routes.health --cov=awf.control.worker.manager --cov=awf.service.smoke --cov=awf.service.worker_heartbeat --cov=awf.db.repositories.system_repo --cov=awf.cli.common --cov-report=term-missing --cov-fail-under=0 -q
```

Result: `104 passed in 35.50s`; `awf.service.worker_heartbeat` reported `100.00%`.

An earlier focused coverage command without `--cov-fail-under=0` hit the repository-configured global `fail-under=99` threshold. I did not run the full `pytest --cov=awf` gate; full AWF/GitHub validation and coverage gating are owned after agent completion.

## Remaining Gaps

None for the saved plan.
