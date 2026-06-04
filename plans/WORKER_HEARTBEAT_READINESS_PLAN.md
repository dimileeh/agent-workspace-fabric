# Worker Heartbeat Readiness Plan

## Problem Statement and Scope

Issue #368 requires AWF's token-free smoke proof to verify real worker liveness. The current smoke path treats `/readyz` `checks.db.ok` as worker readiness, so API + DB health can incorrectly pass when the worker process is not polling or claiming work.

This implementation follows `docs/awf-plans/ws_00dafb138bd04e088a61a495.md` as the detailed contract. Scope is limited to worker heartbeat persistence, `/readyz` `checks.worker`, smoke reason/evidence propagation, focused tests, and smoke documentation.

## Requirements Checklist

- Add a durable worker heartbeat written by the real `ControlWorker` process.
- Surface heartbeat freshness through token-free `/readyz` as `checks.worker`.
- Treat fresh, missing, stale, and unavailable heartbeat lookups with stable reason codes.
- Make smoke read `checks.worker` instead of `checks.db`, preserving reason-code evidence end to end.
- Update CLI/service smoke tests and docs so the proof no longer names `worker_db_substrate`.
- Add focused coverage for worker healthy, missing/stale heartbeat, worker heartbeat writes, and smoke failure evidence.
- Keep the change generic, scoped, non-secret-logging, and unrelated to GC/overlay work.

## Implementation Steps

1. Add failing focused tests for `/readyz` worker heartbeat states, worker heartbeat writes, smoke collector behavior, CLI evidence, and migration graph/schema.
2. Add ORM, migration, repository helpers, and freshness constants.
3. Wire heartbeat upserts into `ControlWorker` polling without letting heartbeat write failures kill the worker loop.
4. Add `/readyz` worker check and include it in readiness status.
5. Update smoke service phase and default collector to consume worker heartbeat evidence.
6. Update `docs/SMOKE_COMMAND.md`.
7. Create `plans/WORKER_HEARTBEAT_READINESS_VALIDATION.md` with focused evidence and note that full AWF/GitHub validation is owned after agent completion.

## Verification Commands and Pass Criteria

Run the narrow checks that cover changed behavior:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/control/test_worker_stop.py tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py tests/unit/db/test_migration_graph.py -q
```

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/routes/health.py src/awf/service/smoke.py src/awf/control/worker/manager.py src/awf/control/worker/config.py src/awf/db/models.py src/awf/db/repositories/system_repo.py src/awf/db/repositories/__init__.py tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/control/test_worker_stop.py tests/unit/service/test_smoke.py tests/unit/cli/test_smoke.py tests/unit/db/test_migration_graph.py
```

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/routes/health.py src/awf/service/smoke.py src/awf/control/worker src/awf/db/repositories/system_repo.py
```

Focused coverage pass criteria: changed modules and new branches are exercised by targeted tests. Full repository validation and the hard AWF coverage gate remain managed by AWF/GitHub after agent completion.
