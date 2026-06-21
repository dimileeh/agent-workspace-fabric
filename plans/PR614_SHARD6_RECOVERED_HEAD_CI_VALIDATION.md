# PR614 Shard 6 Recovered Head CI Validation

Plan reference: `plans/PR614_SHARD6_RECOVERED_HEAD_CI_PLAN.md`

## Requirement Status

- Reproduce the two failing shard-6 tests locally: Complete.
  - The focused command failed before implementation with the same two
    assertions reported by CI.
- Preserve recovered HEAD identity in validation results after filesystem
  recovery, including protected-scope failures: Complete.
  - `pre_push_validation.py` now returns the recovered rejected commit as
    `workspace_head_sha` while cleanup still restores the worktree to the
    recovery anchor.
- Preserve intentional protected commit-blocking cleanup: Complete.
  - The monitor helper regression now asserts the recovery cleanup reset instead
    of treating it as an unexpected extra command.
- Add or adjust focused regression coverage only where behavior changes:
  Complete.
  - Updated existing recovered-head regressions; no broad refactor or unrelated
    coverage padding was added.
- Do not run broad AWF/GitHub validation or full coverage locally: Complete.
  - Only targeted pytest and targeted ruff checks were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
- `plans/PR614_SHARD6_RECOVERED_HEAD_CI_PLAN.md`
- `plans/PR614_SHARD6_RECOVERED_HEAD_CI_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_rename_includes_source_path tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit -q
```

Initial result: failed with the CI assertions. Final result: `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head
```

Result: `4 passed, 12 deselected`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k missing_head_recovery
```

Result: `4 passed, 17 deselected`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py
```

Result: `All checks passed!`

Full AWF/GitHub validation and coverage gates were intentionally not run locally;
AWF owns broad validation after agent completion.
