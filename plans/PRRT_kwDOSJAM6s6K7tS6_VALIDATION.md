# PRRT_kwDOSJAM6s6K7tS6 Validation

## Plan Review

- Added a focused regression for dirty-finalize mirror hook-path repair failure.
- Confirmed the regression failed before the implementation change with
  `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` instead of
  `MIRROR_HOOKS_PATH_POISONED`.
- Added an explicit `_MonitorMirrorHooksPathRepairFailedError` handler in the
  dirty-finalize commit-sink path.

## Focused Checks

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py -q
```

Result: passed (`1 passed`).

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py
```

Result: passed.

## Broad Validation

Full AWF/GitHub validation is intentionally not run during the agent phase per
the AWF workspace contract; AWF owns broad validation, provenance, and merge
gating after agent completion.
