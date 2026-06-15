# Service GC Worker-Delegation Plan

## Problem statement and scope

`awf service gc --execute` resolves entirely inside the API container. That
container runs without `CAP_SYS_ADMIN`, so it cannot unmount/reclaim the
dominant disk consumers — the per-workspace Claude auth overlays (~1.7 GB each)
and the shared `_shared/claude-base` tree. The result (#582) was that an
operator-triggered execute reaped the cheap artifacts (worktrees, compose
stacks, leases) and reported **success while reclaiming zero bytes** of the
real consumers.

Scope: make on-demand `awf service gc --execute` reclaim those
capability-gated paths by delegating that portion of the reap to the worker
(which holds `CAP_SYS_ADMIN`), then fold the worker's actually-reclaimed
bytes/paths back into the gc response so the operator sees real reclamation.
Keep AWF core generic; the dry-run plan must still mirror what `--execute`
would reap.

Out of scope: the periodic worker GC sweep semantics beyond what is needed to
accept on-demand triggers; multi-node fan-out; hosted/GKE delegation.

## Requirements checklist

1. API→worker channel: a `service_gc_requests` table + repository the API
   writes a `pending` row to and the worker claims via
   `SELECT ... FOR UPDATE SKIP LOCKED` (system-scoped, no `workspace_id`,
   mirroring `worker_heartbeats`).
2. `awf service gc --execute` performs the API-side worktree/compose/lease
   reclaim, then delegates the capability-gated reclaim (auth overlays +
   `_shared/claude-base`) to the worker.
3. Worker's reclaimed bytes/paths are folded into the gc response so headline
   totals and `deleted_paths` reflect real reclamation (no double counting,
   no worker-only candidate loss).
4. Dry-run (default) neither triggers the worker nor deletes anything, but
   still runs the discarded-status preview pass and combines reports so the
   plan mirrors what `--execute` reaps.
5. Operator filters (`--min-age-hours`, `--limit`, `--status`,
   `--exclude-status`) are honored by both the API-side and worker-delegated
   passes.
6. Timeout/deadline budgeting: the worker-delegation deadline is reduced by the
   elapsed API-side phase, the CLI HTTP timeout budgets both phases, and a poll
   that observes completion past the deadline reports a timeout rather than
   false success.
7. Terminal-status bookkeeping is correct: `mark_completed`/`mark_failed` must
   not overwrite an `expired` row; stuck `running` rows are recovered; a
   trigger param-parse error is recorded as a terminal failure.
8. Reason codes flow end-to-end and validation provenance/coverage hold; CLI
   and server share one reason-code vocabulary (drift-guarded).

## Implementation steps

1. Add the `ServiceGCRequest` model + Alembic migration
   `b582d1c4e7a9_service_gc_requests` and `service_gc_request_repo` with
   claim/finish/expire helpers and dialect-aware locking.
2. Add the orchestration in `service/gc_request.run_service_gc_request`,
   splitting passes into `gc_terminal_passes`, `gc_claude_base`,
   `gc_worker_delegation`, and `gc_worker_trigger`.
3. Wire the worker side: `control/worker/cleanup_service_gc`,
   `cleanup_auth_overlay`, dispatch + claim of on-demand triggers ahead of the
   periodic reap.
4. Expose the API route + request/response schemas (reused by MCP) and
   regenerate `openapi.json`.
5. Add the `awf service gc` CLI command with the operator filters and
   phase-aware HTTP timeout budgeting.
6. Fold worker results into the response; reconcile per-candidate auth/
   claude-base status and byte totals across passes.

## Verification commands and pass criteria

Narrow first, then widen the touched surface:

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

Pass criteria: targeted suites green; OpenAPI drift gate clean; the broad
99%-coverage gate and full validation suite are owned by AWF/CI after the agent
phase. Dry-run output mirrors `--execute` reaps; execute response headline
bytes/paths reflect worker-reclaimed auth overlays and `_shared/claude-base`.
