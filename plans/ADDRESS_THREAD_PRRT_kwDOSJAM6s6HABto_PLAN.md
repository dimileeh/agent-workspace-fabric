# Address Thread PRRT_kwDOSJAM6s6HABto Plan

## Problem Statement

The review thread reports that `sweep_classified_orphans(..., enabled=False)` still scans Docker resources and managed worktrees before returning a disabled no-op result through `reap_classified_orphans`.

## Scope

- Keep the change limited to `sweep_classified_orphans` disabled behavior.
- Preserve the existing enabled sweep behavior, including concurrent scanner startup.
- Add a focused regression test proving disabled sweeps skip scanner and database work.
- Do not run broad AWF/GitHub-owned validation; record focused checks only.

## Requirements

- [ ] `enabled=False` returns an `OrphanReapResult` with the existing disabled contract.
- [ ] `enabled=False` does not call Docker scanning, worktree scanning, or workspace view loading.
- [ ] Existing enabled sweep behavior remains covered by the current tests.

## Implementation Steps

1. Add a failing unit test for the disabled sweep path.
2. Add an early return at the top of `sweep_classified_orphans`.
3. Run the focused orphan-resource test(s) that cover the new behavior.
4. Write validation notes against this plan.

## Verification

Focused command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py -q
```

Pass criteria:

- The new disabled sweep regression test passes.
- Existing sweep tests in the same file still pass.
- Full AWF/GitHub validation remains owned by AWF after agent completion.
