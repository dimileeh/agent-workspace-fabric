# Sync-Base Mirror Hooks Repair Validation

Plan reference: `plans/SYNC_BASE_MIRROR_HOOKS_PLAN.md`

## Requirement Status

- Repair the worktree mirror hooks path before the sync-base `git merge
  --no-edit` command: Complete.
- If mirror hook repair fails, return a failed `_GitPushResult` with the
  existing mirror-hooks reason code and do not run the merge: Complete.
- Preserve existing sync-base conflict, protected-scope, and validated-push
  behavior: Complete for the touched surface; existing flow remains unchanged
  after the added pre-merge guard.
- Add focused regression tests for ordering and failure behavior: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`
- `plans/SYNC_BASE_MIRROR_HOOKS_PLAN.md`
- `plans/SYNC_BASE_MIRROR_HOOKS_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q
```

Result: passed, `4 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py
```

Result: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation and merge-gate provenance after completion.
