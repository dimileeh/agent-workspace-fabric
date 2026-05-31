# PRRT_kwDOSJAM6s6F7aB7 Commit Autofix Untracked Directory Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F7aB7_commit_autofix_untracked_dir_PLAN.md`

## Requirement Status

- Add a regression test showing an initial untracked directory operation path
  allows a later hook-modified file contained in that directory: Complete.
- Preserve the operation-scope guard for dirty paths outside the initial
  operation, including paths that only share a string prefix with the directory
  name: Complete.
- Preserve the repair-path guard so only deterministic hook-reported worktree
  modifications are restaged: Complete.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F7aB7_commit_autofix_untracked_dir_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F7aB7_commit_autofix_untracked_dir_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_hook_modified_files_inside_untracked_operation_directory -q
```

Result before implementation: failed because the retry was skipped as unsafe
with `dirty_paths=['docs/newdir/file.py']`,
`operation_dirty_paths=['docs/newdir/']`, and
`worktree_modified_paths=['docs/newdir/file.py']`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_hook_modified_files_inside_untracked_operation_directory tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_rejects_paths_outside_untracked_operation_directory -q
```

Result after implementation: passed, `2 passed in 0.68s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Result after implementation: passed, `22 passed in 0.75s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Result after implementation: passed, `2 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/commit_autofix.py
```

Result after implementation: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
