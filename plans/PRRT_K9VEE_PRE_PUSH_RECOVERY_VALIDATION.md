# PRRT_K9VEE Pre-Push Recovery Validation

Plan reference: `PRRT_K9VEE_PRE_PUSH_RECOVERY_PLAN.md`

## Requirement Status

- Add regression coverage for the recovered-HEAD protected-scope rejection path: Complete.
- Restore the worktree to `recovery_head` before returning the protected-scope failure: Complete.
- Preserve the existing reason code and failure message: Complete.
- Keep validation focused; broad AWF/GitHub validation is managed after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head_blocks_committed_protected_scope_violation
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py
```

Result: both passed. The targeted test confirms the protected-scope failure path calls cleanup with `restore_ref=recovery_head` and returns the recovery anchor as the workspace head.

Full AWF/GitHub validation is intentionally left to the AWF-managed post-agent validation phase.
