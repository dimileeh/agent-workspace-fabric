# PRRT_kwDOSJAM6s6F68aG Commit Autofix Untracked Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F68aG_commit_autofix_untracked_PLAN.md`

## Requirement Status

- Add a regression test showing untracked operation paths do not block retrying
  a deterministic hook repair on a tracked file: Complete.
- Preserve the existing operation-scope guard so untracked paths outside the
  monitor operation still block the retry: Complete.
- Preserve the existing safety rule that unrelated tracked worktree-modified
  paths outside the hook repair set block the retry: Complete.
- Restage only hook-modified repair paths, not unrelated untracked paths:
  Complete.
- Keep validation focused; full AWF/GitHub validation is managed after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/PRRT_kwDOSJAM6s6F68aG_commit_autofix_untracked_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F68aG_commit_autofix_untracked_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_untracked_operation_paths -q
```

Result before implementation: failed because the retry was skipped as unsafe
with `worktree_modified_paths` containing both the untracked operation path and
the hook-fixed tracked path.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_allows_untracked_operation_paths tests/unit/runtime/test_pr_monitor_commit_autofix.py::test_monitor_precommit_autofix_retry_rejects_untracked_paths_outside_operation -q
```

Result after implementation: passed, `2 passed in 0.72s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Result after implementation: passed, `12 passed in 0.67s`.

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
