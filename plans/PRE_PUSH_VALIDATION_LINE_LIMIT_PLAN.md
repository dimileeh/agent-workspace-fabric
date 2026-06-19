# Plan: Bring `pre_push_validation.py` and `test_pr_monitor_pre_push_validation_finalize.py` under the first-party line limit

## Problem Statement

CI run `python-coverage-shards (8)` for PR #615 (dimileeh/agent-workspace-fabric)
failed with:

```
AssertionError: assert {
  'src/awf/runtime/pr_monitor_runner/pre_push_validation.py': 1562,
  'tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py': 2535,
} == {}
```

Both first-party files exceed the 1,500-line maintainability limit enforced by
`tests/unit/test_core_decomposition_maintainability.py`
(`MAX_FIRST_PARTY_FILE_LINES = 1_500`).

The previous split (commit `6c8ca0bd7`) extracted `pre_push_validation_fix_pass.py`
and the dirty-finalize tail of the test module into a new sibling
`test_pr_monitor_pre_push_validation_finalize.py`. Subsequent reason-coded
dirty-finalize commits landed additional finalize tests and finalize logic in
those same two files, pushing both back over the limit.

## Scope

Two surgical, mechanical splits:

1. **Source split** — extract the dirty-finalize implementation
   (`_try_finalize_pre_push_dirty_repair_state`,
   `_rollback_finalize_dirty_residue_before_provider_recovery`,
   `_pre_push_validation_cleanup`, `_operation_owned_delta_paths`,
   `_committed_delta_paths`) into a new
   `pre_push_validation_dirty_finalize.py` sub-module. The parent module
   re-exports these symbols to preserve its existing public surface and the
   test monkeypatch semantics (the existing convention used for
   `pre_push_validation_fix_pass.py`).
2. **Test split** — move the dirty-finalize tail of
   `test_pr_monitor_pre_push_validation_finalize.py` (the post-finalize
   regression tests added after the previous split, starting at
   `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`)
   into a new sibling
   `test_pr_monitor_pre_push_validation_finalize_post_commit.py`.

Both splits must stay purely mechanical: no assertion, behavior, or signature
changes; only file moves plus the parent re-export block.

## Requirements

- [ ] `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` <= 1,500 lines.
- [ ] New `pre_push_validation_dirty_finalize.py` <= 1,500 lines.
- [ ] `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` <= 1,500 lines.
- [ ] New `test_pr_monitor_pre_push_validation_finalize_post_commit.py` <= 1,500 lines.
- [ ] All moved functions keep their exact signatures, bodies, and docstrings.
- [ ] The parent `pre_push_validation.py` re-exports the moved finalize symbols
      with `# noqa: F401` so the existing public/monkeypatch surface is
      unchanged.
- [ ] No assertion, behavior, or import-shape changes in any moved test.
- [ ] `tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit` passes.
- [ ] All moved finalize tests still pass (`pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize*.py -q`).
- [ ] `ruff check` and `mypy` clean on the touched src files.

## Implementation Steps

1. Read the boundary of the finalize block in
   `pre_push_validation.py` (lines 510–1170) and confirm the symbol set to
   extract: `_pre_push_validation_cleanup`,
   `_operation_owned_delta_paths`, `_committed_delta_paths`,
   `_try_finalize_pre_push_dirty_repair_state`,
   `_rollback_finalize_dirty_residue_before_provider_recovery`.
2. Create `pre_push_validation_dirty_finalize.py` with the moved functions,
   importing the helpers it depends on (`_log`, `_changed_paths_from_name_status_z`,
   `git_worktree_command`, the finalize constants, the
   `ValidationWorktreeCheck`/`ValidationWorktreeCleanup` lazy import) from their
   existing locations.
3. Replace the moved block in `pre_push_validation.py` with a re-export import
   block (`from ...pre_push_validation_dirty_finalize import (...)` with
   `# noqa: F401`).
4. Re-check the parent line count; if it is still over the limit, fall back to
   also moving `_run_pre_push_validation` and the run-persistence helpers
   (`_start_pre_push_validation_run`, `_finish_pre_push_validation_run`) into
   a second new sub-module (kept as a contingency in the validation file).
5. Split the test module: read the test boundary at
   `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`
   (line 1764) and move it through the end of the file into the new sibling
   `test_pr_monitor_pre_push_validation_finalize_post_commit.py`, copying the
   module header (docstring + imports + `factory` fixture).
6. Run the focused verification commands.

## Verification

```bash
# Reproduce the maintainability gate
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q

# Confirm moved finalize tests still pass
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py -q

# Lint + types on the touched source
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner tests/unit/runtime
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner
```

Pass criteria:
- `test_first_party_code_files_stay_under_line_limit` passes.
- All moved finalize tests pass.
- No new first-party file exceeds 1,500 lines.
- No broad coverage gate is run locally (AWF/GitHub CI owns that).
