# Validation: `pre_push_validation.py` + `test_pr_monitor_pre_push_validation_finalize.py` line limit

## Plan Reference

`plans/PRE_PUSH_VALIDATION_LINE_LIMIT_PLAN.md`

## Requirement Status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` <= 1,500 lines | Complete | 921 lines after split. |
| 2 | New `pre_push_validation_dirty_finalize.py` <= 1,500 lines | Complete | 703 lines. |
| 3 | `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` <= 1,500 lines | Complete | 1,219 lines after split. |
| 4 | New `test_pr_monitor_pre_push_validation_finalize_post_commit.py` <= 1,500 lines | Complete | 1,352 lines. |
| 5 | Moved functions keep exact signatures/bodies/docstrings | Complete | Mechanical move; only the late-import resolution of `_pre_push_validation_worktree_check` / `_pre_push_validation_cleanup` via the parent namespace was added to preserve monkeypatch semantics (mirrors `pre_push_validation_fix_pass.py`). |
| 6 | Parent re-exports moved finalize symbols with `# noqa: F401` | Complete | `pre_push_validation.py` import block re-exports `_committed_delta_paths`, `_operation_owned_delta_paths`, `_rollback_finalize_dirty_residue_before_provider_recovery`, `_try_finalize_pre_push_dirty_repair_state`. |
| 7 | No assertion/behavior/import-shape change in moved tests | Complete | Tests moved verbatim; `_name_status_z` local helper hoisted into `tests/unit/runtime/_pre_push_validation_helpers.py` and imported by both split test files. |
| 8 | `test_first_party_code_files_stay_under_line_limit` passes | Complete | `pytest tests/unit/test_core_decomposition_maintainability.py -q` -> 9 passed. |
| 9 | Moved finalize tests still pass | Complete | `pytest ...test_pr_monitor_pre_push_validation_finalize.py ...finalize_post_commit.py -q` -> 32 passed. |
| 10 | `ruff check` and `mypy` clean on touched src | Complete | `ruff check src/awf tests` -> All checks passed; `mypy src/awf/runtime/pr_monitor_runner` -> Success: no issues found in 41 source files. |

## Commands Run

```bash
# Reproduce the maintainability gate (previously failing)
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
# -> 9 passed

# Confirm moved finalize tests still pass
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py -q
# -> 32 passed

# Confirm sibling pre_push_validation test surface still passes
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_pre_push_validation.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ \
  tests/unit/runtime/test_pr_monitor_remote_ops.py \
  tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q
# -> 111 passed

# Lint + types on the touched source
uv run --python 3.12 --extra dev ruff check src/awf tests
# -> All checks passed
uv run --python 3.12 --extra dev ruff format --check \
  src/awf/runtime/pr_monitor_runner/pre_push_validation.py \
  src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py \
  tests/unit/runtime/_pre_push_validation_helpers.py
# -> all files already formatted
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner
# -> Success: no issues found in 41 source files
```

## Line Count Summary

| File | Before | After |
|------|--------|-------|
| `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` | 1,562 | 921 |
| `src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py` | — | 703 |
| `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` | 2,535 | 1,219 |
| `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py` | — | 1,352 |
| `tests/unit/runtime/_pre_push_validation_helpers.py` | 269 | 280 |

## Root Cause

The previous split (commit `6c8ca0bd7`) extracted `pre_push_validation_fix_pass.py`
and the dirty-finalize tail of the test module into
`test_pr_monitor_pre_push_validation_finalize.py`. Subsequent reason-coded
dirty-finalize commits (PRRT threads `...6KXLaI`, `...6KYd-r`, `...6KZjtR`,
`...6KZP8f`, `...6KaAWk`, `...6KaUHP`, `...6Ka0aK`, `...6Ka0aO`, `...6KbbE6`,
`...6Kc_Ak`, `...6KcSj`, `...6KdVXx`, `...6KewGH`, `...6KhtZJ`, `...6Khuvf`)
landed additional finalize logic and finalize tests in those same two files,
pushing both back over the 1,500-line first-party limit. The maintainability
gate (`test_first_party_code_files_stay_under_line_limit`) is a CI-required
check, so its failure blocked PR #615.

## Fix

Two surgical, mechanical splits:

1. **Source split** — extracted `_operation_owned_delta_paths`,
   `_committed_delta_paths`, `_try_finalize_pre_push_dirty_repair_state`,
   `_rollback_finalize_dirty_residue_before_provider_recovery` into
   `pre_push_validation_dirty_finalize.py`. The parent re-exports them
   (`# noqa: F401`) to preserve its existing public surface and test
   monkeypatch semantics. `_pre_push_validation_cleanup` and
   `_pre_push_validation_worktree_check` remain in the parent (shared with
   `_run_pre_push_validation` and monkeypatched via the parent namespace); the
   new sub-module resolves them through the parent namespace at call time
   (late import), mirroring the convention in `pre_push_validation_fix_pass.py`.
2. **Test split** — moved the provider-recovery propagation + post-commit
   re-validation + rename/non-ASCII/untracked regression tests (from
   `test_pre_push_validation_finalize_propagates_provider_recovery_retry`
   onward) into
   `test_pr_monitor_pre_push_validation_finalize_post_commit.py`, copying the
   module header (docstring + imports + `factory` fixture). The local
   `_name_status_z` helper was hoisted into the shared
   `_pre_push_validation_helpers.py` module and imported by both split files.

No assertion, behavior, or signature was changed.

## Conclusion

All planned requirements are complete. No iteration needed. Broad coverage
gate (`pytest --cov` / `--cov-fail-under`) is owned by AWF/GitHub CI after
agent completion and was not run locally per the AWF workspace contract.
