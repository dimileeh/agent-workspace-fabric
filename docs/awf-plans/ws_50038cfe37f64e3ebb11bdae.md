# P1 Control-Plane Restart Recovery Hardening Plan

## Scope Summary

Harden worker restart recovery for persisted `monitoring_pr` workspaces that still carry stale execution-claim state from the worker that originally executed the feature. Recovery must keep PR monitoring as monitor-owned work, clear only irrelevant stale execution ownership, preserve monitor-claim semantics, emit durable recovery evidence, and remain idempotent across repeated worker polls/restarts.

## Intended Files And Modules To Touch

- `tests/unit/control/test_worker.py`
  - Add focused regression tests around `ControlWorker.run_once()` / monitor recovery for `monitoring_pr` rows with stale `execution_claimed_by` and `execution_claim_expires_at` values.
  - Reuse existing `_create_monitoring_pr`, `_RecordingExecutor`, blocking executor, `WorkspaceRepository`, `WorkspaceEventRepository`, and `OperationRepository` fixtures/patterns.
- `src/awf/control/worker.py`
  - Update the monitor-recovery claim path so a successfully recovered `monitoring_pr` workspace clears or expires stale execution-claim fields without clearing the active monitor claim.
  - Extend `_monitor_recovery_payload()` / `workspace.monitor_recovery_started` event payload with explicit claim-cleanup details.
  - Keep recovery dispatch order monitor-first, before ready execution, and ensure no full `execute()` call happens for `monitoring_pr` rows.
- `src/awf/db/repositories.py`
  - Prefer avoiding this file because of the active `src/awf/db/**` overlap warning, but touch it narrowly if the worker needs an atomic repository update that claims `monitoring_pr` and clears stale execution fields in one DB transaction.
  - If touched, update only `WorkspaceRepository.claim_monitoring_pr()` or add a very small companion helper; avoid broad repository refactors.
- `tests/unit/db/test_repository_coverage.py`
  - Touch only if repository changes introduce uncovered branches that need direct repository coverage beyond the worker regression tests.

## Tests To Write First

1. Add a failing worker regression for restart recovery with a stale execution claim:
   - Seed a persisted `monitoring_pr` workspace with an open PR, `execution_claimed_by="dead-execution-worker"`, and an expired `execution_claim_expires_at`.
   - Run a fresh `ControlWorker` with a blocking `resume_pr_monitor()` so the DB can be inspected while recovery is active.
   - Assert `executor.resume_calls == [workspace_id]` and `executor.calls == []`.
   - Assert the workspace remains `monitoring_pr`, stale execution claim fields are cleared, and the monitor claim is owned by the recovering worker while the monitor loop is active.
   - Assert the remonitor operation and `workspace.monitor_recovery_started` event include a structured cleanup payload such as `claim_cleanup.execution_claim.action == "cleared_stale"`, previous owner/expiry, and no monitor-claim cleanup action.

2. Add a failing idempotency/concurrency regression:
   - Start worker A on the stale-execution-claim `monitoring_pr` row with a blocking monitor resume.
   - Run worker B while worker A's monitor claim is still active.
   - Assert worker B dispatches zero work, does not call `resume_pr_monitor()`, does not create another remonitor operation/event, and does not clear or overwrite worker A's active monitor claim.
   - Release worker A and assert the final operation succeeds and claim fields are clean.

3. Add a narrow no-op regression for non-stale execution claims if needed:
   - Seed `monitoring_pr` with a future execution claim and an expired/missing monitor claim.
   - Decide from the implementation contract whether to leave the future execution claim untouched but report it as `preserved_unexpired`, or expire it explicitly as irrelevant to monitor recovery.
   - The preferred behavior is conservative: clear only stale execution claims and surface any unexpired execution claim in the recovery payload for operator visibility.

4. If a repository helper is added/changed, add a repository-level test proving:
   - `claim_monitoring_pr()` still refuses rows with an active monitor lease.
   - The stale execution claim cleanup happens only when the monitor claim is successfully acquired or through an explicitly scoped cleanup helper.

## Implementation Outline

1. Reproduce the failing behavior with the first worker test: current recovery resumes PR monitoring but leaves stale `execution_claimed_by` / `execution_claim_expires_at` attached to the `monitoring_pr` row and the recovery payload has only `previous_claim`.
2. Add a small claim-classification helper in `worker.py` that compares execution claim expiry to `now` and returns a structured cleanup payload: previous owner, previous expiry, action (`cleared_stale`, `preserved_unexpired`, or `none`), and reason code.
3. During `_claim_monitoring_pr()` after `WorkspaceRepository.claim_monitoring_pr()` succeeds, clear `ws.execution_claimed_by` and `ws.execution_claim_expires_at` only when the execution claim is stale/expired/missing-owner according to the helper. Do not clear `monitor_claimed_by` or `monitor_claim_expires_at`; they must represent the active monitor lease.
4. Include the cleanup payload in the `OperationType.remonitor` operation payload and `workspace.monitor_recovery_started` event payload. Keep the existing `previous_claim` snapshot for compatibility.
5. If ORM state after the repository update is unreliable or race-prone, replace the worker-local mutation with a narrow repository method that performs monitor-claim acquisition and stale execution-claim cleanup under the same transaction, while preserving the active monitor claim filter.
6. Keep `_list_monitoring_pr()` / `list_schedulable_ids()` behavior unchanged unless tests show claimed rows block later unclaimed rows; the existing active monitor lease filter is part of the duplicate-loop protection.
7. Preserve current monitor recovery operation finishing semantics: success/failure is still driven by `_safely_resume_pr_monitor()`, and execution claims are released independently from execution tasks.

## Validation Commands

Run the narrow tests first, then the broader surfaces justified by touching the worker and possibly repository:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_repository_coverage.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

If the final change touches only `src/awf/control/worker.py` and `tests/unit/control/test_worker.py`, the repository-coverage command can be skipped and documented as not applicable. If coverage shifts unexpectedly, run:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks And Assumptions

- There is an advisory overlap on `src/awf/db/**` with workspace `ws_fcee67cfbc274297b5b692df`. I will avoid DB repository edits unless atomicity requires them; if repository edits are needed, keep them minimal and expect possible `STALE_OVERLAP` revalidation.
- The stale execution claim is treated as irrelevant once a workspace is already `monitoring_pr`; monitor recovery must not transition the row back to `ready` or `running`.
- The active monitor claim is the duplicate-loop guard. Cleanup code must never clear another worker's unexpired monitor claim or create a second remonitor operation while one worker is actively resuming the monitor.
- Existing event name `workspace.monitor_recovery_started` and reason code `MONITOR_RECOVERY_AFTER_RESTART` are assumed to remain the durable recovery surface; the change should extend payloads rather than introduce a parallel event unless tests expose a need for a separate cleanup-only event.
- Time comparisons need to handle naive PostgreSQL datetimes the same way existing `_execution_claim_is_stale()` / `_monitor_claim_is_stale()` helpers do.

## Explicit Non-Goals

- No branch switching, pushing, rebasing, or PR operations.
- No backlog-ledger edits in `TODO/pre-gke-industrial-readiness.md`; the operator will reconcile after merge.
- No console/API UI work unless the existing API automatically surfaces operation/event payloads.
- No scheduler redesign, resource reservation schema changes, or migration work.
- No changes to provider fallback/circuit breaker behavior, PR monitor decision logic, or merge-gate policy.
