# Review 4620252998 Terminal GC Side-Effect Gate Plan

## Problem Statement And Scope

Greptile noted that `run_terminal_workspace_gc` now runs compose teardown before
secret-lease revocation and resource-reservation release, but lacks a dedicated
batch terminal-GC regression proving a failed compose teardown blocks those side
effects for that workspace.

Scope is limited to the missing regression coverage. No GC implementation change
is planned unless the regression exposes a behavior gap.

## Requirements Checklist

- Add a focused unit test for `run_terminal_workspace_gc` with a failed compose
  teardown.
- Seed both an active secret lease and an active resource reservation for the
  failed-teardown workspace.
- Assert the failed workspace keeps its lease and reservation, and no release
  summaries are emitted for it.
- Include a successful candidate in the same batch to prove other workspaces are
  not blocked.
- Run only the targeted new test; broad AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add the regression test alongside existing terminal-GC lease tests.
2. Reuse existing helpers for workspace creation, secret-lease issuance, and
   reservation creation.
3. Run the new focused pytest node.
4. Record validation evidence in a matching validation document.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_batch_terminal_gc_compose_teardown_failure_blocks_runtime_side_effects -q
```

Pass criteria: the targeted test passes and confirms only the successful
workspace releases runtime side effects.
