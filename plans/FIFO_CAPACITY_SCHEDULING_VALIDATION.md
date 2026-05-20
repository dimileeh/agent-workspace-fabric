# FIFO Capacity Scheduling Validation

Plan reference: `plans/FIFO_CAPACITY_SCHEDULING_PLAN.md`

## Requirement Status

- Preserve non-blocking workspace admission: Complete. No admission path changes were made; capacity is enforced only at worker claim time.
- Keep PRD priority classes/effective score ahead of FIFO: Complete. Requested candidates still use the existing scheduler score ordering, with FIFO as the existing score tie-break.
- Gate `requested -> provisioning` on configured local resource limits: Complete. `ControlWorker` gates requested claims when local CPU, memory, or DinD capacity limits are configured.
- Treat allocated runtime statuses separately from queued `requested` demand: Complete. Allocated totals use `provisioning`, `ready`, `running`, `validating`, `pushing`, `monitoring_pr`, and `destroying`.
- Do not block on unknown/unconfigured capacity dimensions: Complete. The worker capacity gate is inactive when no local capacity limit is configured and ignores dimensions with `None` limits.
- Select oldest satisfiable requested workspace when older work cannot fit: Complete. The worker overfetches requested candidates when capacity limits are active and can dispatch a younger fitting candidate after recording the older deferral.
- Record local capacity decision reasons: Complete. Added `LOCAL_CAPACITY_DEFERRED`, `LOCAL_CAPACITY_UNSATISFIABLE`, and `LOCAL_CAPACITY_RESERVATION_DEFAULTED` queue decision reasons.
- Make capacity claim atomic across workers on the same local node: Complete. Capacity-gated requested claims run under a PostgreSQL transaction advisory lock scoped by local node id.
- Expose queue/capacity visibility: Complete. Resource saturation API/OpenAPI now includes `allocated_resources`, `allocated_capacity`, and `capacity_queue`; the console capacity panel shows allocated CPU, queue depth, oldest queued age, allocated capacity meters, and queue blocker counts.
- Add focused regression tests: Complete. Added worker tests for deferral, oldest-satisfiable dispatch, priority preservation, concurrent capacity claims, and metrics visibility.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/db/repositories.py`
- `src/awf/service/metrics.py`
- `src/awf/api/routes/metrics.py`
- `src/awf/service/config.py`
- `src/awf/service/worker.py`
- `apps/console/components/console-dashboard.tsx`
- `apps/console/lib/types.ts`
- `tests/unit/control/test_worker.py`
- `tests/unit/api/test_metrics_capacity.py`
- `openapi.json`

Validation commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_defers_when_allocated_capacity_full tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_dispatches_oldest_satisfiable_candidate tests/unit/control/test_worker.py::TestRunOnce::test_requested_capacity_gate_preserves_scheduler_priority_before_fifo tests/unit/control/test_worker.py::TestRunOnce::test_concurrent_capacity_claims_do_not_oversubscribe_requested_workspaces -q` - passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure -q` - passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q` - passed, 185 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q` - passed, 11 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` - passed.
- `uv run --python 3.12 --extra dev mypy src/awf` - passed.
- `python scripts/generate_openapi.py --check` - failed in the bare Python environment because `fastapi` was not installed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py` - regenerated `openapi.json`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check` - passed.
- `npm --prefix apps/console run lint` - passed.
- `npm --prefix apps/console run typecheck` - passed.
- `npm --prefix apps/console run build` - passed.
- `uv run --python 3.12 --extra dev pytest tests/unit -q` - passed, 6510 tests.

## Remaining Gaps

None for this local-node FIFO capacity scheduling slice. Multi-node scheduling remains explicitly deferred by the original plan.
