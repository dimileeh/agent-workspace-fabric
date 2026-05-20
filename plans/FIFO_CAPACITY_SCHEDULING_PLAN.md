# FIFO Capacity Scheduling Plan

## Problem Statement And Scope

AWF already accepts valid workspace requests while worker provision slots are full, but requested workspaces are selected primarily by PRD class/effective score with FIFO only as a tie-break. Local CPU, memory, disk, and DinD reservation pressure is visible but not a dispatch gate. The change adds first-class local resource FIFO scheduling: keep request admission non-blocking, dispatch the oldest satisfiable requested workspace within PRD priority order, and defer capacity-blocked workspaces with auditable queue decisions.

This plan is scoped to the local-node scheduler. Multi-node scheduling and hard admission rejection for full local capacity are explicitly deferred.

## Requirements Checklist

- Preserve non-blocking `POST /v1/workspaces` admission for valid requests; do not reject solely because worker slots or local resources are full.
- Keep PRD scheduler priority classes and effective score ordering ahead of FIFO, then apply FIFO within the resulting class/score band.
- Gate `requested -> provisioning` claims on configured local resource limits, using allocated workspaces as runtime demand and queued `requested` reservations as planned demand.
- Treat `provisioning`, `ready`, `running`, `validating`, `pushing`, `monitoring_pr`, and `destroying` reservations as allocated capacity.
- Do not block on unknown or unconfigured capacity dimensions.
- Select the oldest satisfiable requested workspace when an older queued workspace cannot currently fit, and record the older workspace deferral.
- Record explicit queue decision reasons for local capacity deferral, unsatisfiable local capacity, and missing/defaulted reservation data.
- Make the capacity claim atomic across concurrent workers on the same local node.
- Expose queue/capacity visibility through the resource saturation API and console without breaking existing clients.
- Add focused regression tests for deferral, oldest-satisfiable dispatch, priority preservation, concurrency, and metrics visibility.

## Implementation Steps

1. Add worker tests that prove the current scheduler can oversubscribe capacity or block younger satisfiable work, then implement against those tests.
2. Add repository helpers for latest reservations by workspace and allocated-capacity totals by active runtime statuses.
3. Extend worker configuration with local capacity and reservation default values from settings.
4. Overfetch requested scheduler candidates, then atomically claim only candidates that fit local allocated capacity.
5. Add local capacity queue decisions and capacity summaries to preserve auditability.
6. Add resource saturation visibility for allocated resources and queued capacity pressure.
7. Update the console resource panel to surface allocated capacity and queue pressure.
8. Run targeted tests first, then the repository validation commands that cover touched Python and console surfaces.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`
- `uv run --python 3.12 --extra dev pytest tests/unit -q`
- `python scripts/generate_openapi.py --check`
- `npm --prefix apps/console run lint`
- `npm --prefix apps/console run typecheck`

Pass criteria: targeted FIFO/capacity tests pass, broader unit/type/lint/spec checks pass or any unrelated pre-existing failure is documented in the validation file.
