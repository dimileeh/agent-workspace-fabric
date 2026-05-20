# P0 Preserved Active Execution Recovery Plan

## Problem Statement And Scope

Implement the saved AWF plan in `docs/awf-plans/ws_77bb4cce4aea4892bb41e0e6.md`: a preserved active execution after worker restart must trigger AWF-owned recovery instead of passively waiting until stale-active cleanup fails useful work.

This slice is limited to the worker/executor control-plane path and focused unit coverage for preserved active recovery, monitor reattachment, replacement creation, operator-recoverable ambiguity, and true orphan cleanup.

## Requirements Checklist

- Treat `ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART` as the start of recovery.
- Recover existing PRs by attaching exactly one PR monitor.
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

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/service -q
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control tests/unit/service
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: new regressions fail before implementation and pass afterward; no duplicate recovery artifacts appear after repeated scans; stale-active failure remains intact for true orphan cases.
