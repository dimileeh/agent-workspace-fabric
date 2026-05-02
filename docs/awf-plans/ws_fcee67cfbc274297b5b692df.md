# AWF Plan: P1 Scheduler Backlog Fairness And Decision Explanations

## Scope
Implement the remaining P1 scheduler backlog slice for deterministic queue scoring, starvation prevention, retry-aware scoring, human-escalation/operator boost, and durable machine-readable scheduler explanations. This builds on the existing `queue_decisions` and `resource_reservations` records instead of replacing them.

This planning phase only creates this artifact. Implementation will follow strict TDD.

## Current Observations
- `WorkspaceRepository.list_schedulable_ids()` currently orders schedulable work by `Workspace.created_at ASC, Workspace.id ASC`.
- `create_workspace_v2_row()` already writes a basic admitted `QueueDecision`, but the record lacks a full score breakdown and has no human boost or retry/backoff context.
- `WorkspaceV2Task.priority` exists, but priority is not stored as durable scheduler input except indirectly in `computed_priority`.
- Owned-path overlap is already advisory via coordination warnings and overlap risk summaries; this slice must keep that behavior.
- Advisory overlap warning: `src/awf/api/schemas.py` overlaps active workspace `ws_6c58298db1c14c8cb6a6f906`; if that workspace lands first, revalidate/rebase per `STALE_OVERLAP`, but do not treat the overlap as an admission blocker.

## Intended Files And Modules To Touch
- `src/awf/service/scheduler.py` (new): pure scoring model and helpers for `class_priority`, class bias, explicit priority, age boost, retry bonus, human boost, ordering tuple, and explanation payloads.
- `src/awf/service/workspaces.py`: replace local scoring helpers with the shared scheduler helper; persist scheduler input metadata in `task_policy`; include retry lineage/backoff context in retry-created workspaces and queue decisions.
- `src/awf/db/models.py`: add an additive `QueueDecision.score_summary` JSON field if needed for durable machine-readable explanations without adding many scalar columns.
- `src/awf/db/repositories.py`: extend `QueueDecisionRepository.create()` and scheduler listing/order logic; keep `list_schedulable_ids()` compatibility while applying score order for requested/ready/monitoring candidates.
- `src/awf/control/worker.py`: record durable `ordered` decisions when candidates are selected for provisioning/execution/monitor resumption and `deferred` decisions when provider cooldown/circuit-breaker backoff suppresses a candidate.
- `src/awf/api/schemas.py`: expose the score explanation payload on `latest_queue_decision` in an additive, machine-readable field.
- `migrations/versions/<new>_queue_decision_score_summary.py`: only if `score_summary` requires a schema migration.
- `tests/unit/service/test_scheduler_scoring.py` (new): focused pure scoring tests.
- Existing tests to update/add: `tests/unit/service/test_scheduler_records.py`, `tests/unit/db/test_scheduler_records.py`, `tests/unit/db/test_workspace_repository.py`, `tests/unit/control/test_worker.py`, `tests/unit/api/test_workspaces.py`, and `tests/unit/db/test_migration_graph.py` if a migration is added.

## Tests To Write First
1. Pure scheduler scoring in `tests/unit/service/test_scheduler_scoring.py`:
   - older queued work receives increasing age boost and the boost is capped;
   - task class priority and class bias match PRD ordering (`migration_task` through `docs_task`);
   - explicit priority changes effective dispatch score predictably;
   - retry bonus is applied only for parent attempts that failed with `infrastructure_failure`;
   - retry/backoff context produces an explanation showing not-before suppression without awarding an active dispatch advantage;
   - human-escalation boost is bounded to the policy max (`+5`) and appears in the score breakdown;
   - final ordering tuple is deterministic: higher class priority, higher effective score, earlier queued time, stable id tie-breaker.

2. Repository ordering in `tests/unit/db/test_workspace_repository.py`:
   - `list_schedulable_ids()` orders candidates by scheduler score instead of raw creation order;
   - age/fairness lets old same-class work outrank younger same-class work that would otherwise starve;
   - class priority remains lexicographic per PRD and owned-path overlap is not an admission/listing blocker;
   - provider-cooldown or circuit-breaker suppressed rows do not consume the selected limit.

3. Durable decision records in `tests/unit/db/test_scheduler_records.py` and `tests/unit/service/test_scheduler_records.py`:
   - admitted decisions include `score_summary` with base priority, class bias, age boost, retry bonus, human boost, effective score, queued timestamp, and ordering tuple;
   - ordered decisions are appended when the worker actually selects a candidate;
   - deferred decisions are appended with reason code and retry/backoff context when a candidate is skipped due to cooldown/circuit breaker;
   - payloads are bounded, JSON-serializable, and contain no prompt text, tokens, or secrets.

4. Worker behavior in `tests/unit/control/test_worker.py`:
   - provisioning and ready execution dispatch the highest scored work first;
   - retry-created infrastructure-failure work gets the small retry boost but does not outrank materially higher priority new work;
   - human-boosted work is selected ahead of equal-class/equal-priority work;
   - owned-path overlaps still only produce warnings/decision payload risk summaries, not blocked admission.

5. API exposure in `tests/unit/api/test_workspaces.py`:
   - workspace detail includes the latest queue decision with the score explanation payload;
   - existing clients still receive current scalar fields (`computed_priority`, `age_boost`, `retry_bonus`, etc.);
   - deferred/ordered/admitted decisions remain machine-readable through `latest_queue_decision`.

## Implementation Steps
1. Add the pure scheduler scoring helper and constants, preserving existing PRD values: class priority, class bias, retry bonus `+3`, human boost max `+5`, and a bounded age/fairness boost.
2. Store scheduler inputs in workspace task policy on create/retry so later worker polls can recompute scores without depending on request-only data.
3. Extend queue-decision persistence with `score_summary` if current scalar columns cannot express human boost and retry/backoff context cleanly.
4. Update admitted decision creation to use the shared helper and include the full explanation payload.
5. Update repository/worker scheduling selection to apply the score ordering while preserving `SKIP LOCKED` behavior and current status rechecks.
6. Record `ordered` decisions when work is selected and `deferred` decisions when cooldown/backoff/circuit-breaker suppression prevents dispatch.
7. Add API schema exposure for the additive score explanation field.
8. Keep owned-path overlap advisory-only throughout; do not add any hard blocker based on overlap.

## Validation Commands
Run narrow tests first, then the broader configured Python checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_scheduler_scoring.py tests/unit/service/test_scheduler_records.py tests/unit/db/test_scheduler_records.py tests/unit/db/test_workspace_repository.py tests/unit/control/test_worker.py tests/unit/api/test_workspaces.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

If a migration is added, also run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q
```

Coverage command if the implementation touches broad scheduler/worker behavior:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks
- Keeping SQL row locking and Python-level score ordering consistent may be subtle; tests must cover Postgres `SKIP LOCKED` statement shape and SQLite behavior.
- Existing API consumers may assume only `admitted` decisions exist. New `ordered` and `deferred` records must be additive and preserve current scalar fields.
- Age/fairness can create unexpected ordering changes if not capped and documented in the payload.
- Retry/backoff state is split between workspace failure fields and provider recovery policy; the score helper must handle missing or legacy metadata safely.
- The active overlap on `src/awf/api/schemas.py` may require revalidation if target-branch changes land first.

## Assumptions
- The PRD formula remains authoritative: `priority + class_bias + age_boost + retry_bonus + human_boost`, ordered lexicographically by class priority, effective score, then queued time.
- The human-escalation boost can be represented as an explicit bounded scheduler input in task policy/API without adding a separate operator-control endpoint in this slice.
- `Workspace.created_at` is sufficient as the queued timestamp for existing workspaces; legacy rows without score metadata fall back to zero priority/class defaults and creation-order ties.
- Provider cooldown/circuit-breaker suppression counts as a scheduler defer explanation, not an admission failure.

## Explicit Non-Goals
- No GKE or multi-node scheduler implementation.
- No exclusive-lock system and no hard admission blocking for owned-path overlap.
- No console UI redesign; API/console surfacing can remain the existing `latest_queue_decision` machine-readable payload.
- No backlog ledger edits in `TODO/pre-gke-industrial-readiness.md`.
- No weakening validation, coverage, merge, or quality gates.
