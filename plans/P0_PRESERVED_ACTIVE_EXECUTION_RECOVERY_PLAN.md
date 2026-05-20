# P0 Preserved Active Execution Recovery Plan

## Problem Statement And Scope

Implement the saved AWF plan in `docs/awf-plans/ws_77bb4cce4aea4892bb41e0e6.md`: a preserved active execution after worker restart must trigger AWF-owned recovery instead of passively waiting until stale-active cleanup fails useful work.

This slice is limited to the worker/executor control-plane path and focused unit coverage for preserved active recovery, monitor reattachment, replacement creation, operator-recoverable ambiguity, and true orphan cleanup.

## Requirements Checklist

- Treat `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART` as the start of recovery.
- Recover existing PRs by attaching exactly one PR monitor.
- Iteration 1: recover already-pushed branches by resolving `remote_push_branch`
  or `branch_name` to exactly one open PR when `pr_url`/`pr_number` are absent,
  persist the PR metadata, and attach exactly one PR monitor.
- Iteration 1: classify branch-to-open-PR lookup failures or multiple matching
  PRs as explicit operator-recoverable ambiguity without stale-active failure.
- Recover clean committed work by creating an idempotent validation continuation and dispatching executor recovery.
- Create exactly one lineage-preserving replacement workspace when no usable work or PR exists.
- Leave ambiguous cases in explicit operator-recoverable state without failing or releasing runtime.
- Preserve stale-active failure only for conclusive orphan/no-recovery cases.
- Preserve reason codes, events, operations, and task/attempt lineage.
- Keep recovery idempotent across repeated scans/restarts.

## Implementation Steps

1. Add focused failing worker tests for the required scenarios.
2. Add preserved-active salvage reason codes and idempotent event/operation helpers in `src/awf/control/worker.py`.
3. Add an executor continuation protocol method and worker dispatch path for clean committed work.
4. Add conservative worktree classification using git commands.
5. Add lineage-preserving replacement creation for no-work cases.
6. Gate stale-active cleanup behind conclusive salvage not-possible evidence.
7. Run the requested validation commands and record results in the validation artifact.

## Iteration 1 Plan: Pushed Branch Open-PR Salvage

1. Add focused worker tests first for:
   - active preserved workspace with no PR metadata whose `remote_push_branch`
     resolves to exactly one open PR, including a second scan proving no
     duplicate monitor resume or salvage event;
   - fallback to `branch_name` when `remote_push_branch` is missing;
   - lookup failure and multiple matching open PRs, both recorded as one
     operator-recoverable salvage event without stale-active failure.
2. Add an injectable branch-to-open-PR resolver to `ControlWorker` so unit tests
   remain deterministic and production can use the existing `gh` command runner
   wiring.
3. Before worktree classification in `_recover_preserved_active_execution`, try
   the resolver for the resolved pushed branch. If exactly one open PR is found,
   persist `pr_url`, `pr_number`, `remote_push_branch`, and head SHA metadata
   where available, then reuse the existing PR monitor salvage path.
4. Preserve idempotency by treating the existing
   `ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED` event as the duplicate guard and
   by not invoking the resolver after PR metadata has already been persisted.
5. Keep ambiguous lookup outcomes in
   `runtime_preserved_operator_recovery_required` with reason-coded payloads.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/service -q
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control tests/unit/service
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: new regressions fail before implementation and pass afterward; no duplicate recovery artifacts appear after repeated scans; stale-active failure remains intact for true orphan cases.
