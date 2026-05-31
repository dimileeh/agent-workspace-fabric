# Issue #337 Validation

Plan reference: `plans/ISSUE_337_PLAN.md`

## Requirement Status

- Complete: Regression coverage for a monitor comment-repair commit where
  `end-of-file-fixer` reports `files were modified by this hook`, the monitor
  re-stages the autofixed path, retries `git commit` once, succeeds, and the
  next dirty-worktree guard sees a clean worktree.
- Complete: Regression coverage for an autofix retry whose second commit still
  fails; the monitor retries once and returns `False`.
- Complete: Regression coverage for unsafe dirty paths. An unowned path left
  dirty after the failed commit is not re-staged, and the next pass still returns
  `PRE_EXISTING_DIRTY_WORKTREE`.
- Complete: Regression coverage for a non-autofixable unknown hook that reports
  a modified file; the monitor does not treat that as a safe deterministic
  autofix.
- Complete: The monitor retry path reuses the executor commit classifier from
  `awf.control.executor.quality_gates` rather than adding hook-specific regexes
  or lists.
- Complete: `_pre_existing_dirty_repair_worktree_result` was not changed; its
  conservative guard behavior is preserved.
- Complete: `src/awf/runtime/pr_monitor_runner/remote_repair.py` remains below
  the 1500-line split guard by moving the retry helper into
  `src/awf/runtime/pr_monitor_runner/commit_autofix.py`.

## Files Changed

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
- `plans/ISSUE_337_PLAN.md`
- `plans/ISSUE_337_VALIDATION.md`

## Evidence

TDD failure evidence before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -k "restages_precommit_autofix or autofix_retry_still_fails or unowned_autofix" -q
# 3 failed, 19 deselected
```

Additional safety failure before tightening unknown-hook classification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -k "unknown_autofix_hook" -q
# 1 failed, 22 deselected
```

Focused passing tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -q
# 43 passed
```

Focused static checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py
# All checks passed
```

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py
# 3 files already formatted
```

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/commit_autofix.py
# Success: no issues found in 2 source files
```

Line split check:

```bash
wc -l src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/commit_autofix.py
# 1480 remote_repair.py
# 101 commit_autofix.py
```

Full AWF/GitHub validation, full coverage, full-repository test suites, frontend
builds, pushing, and PR creation were not run in this agent phase. They are
owned by AWF/GitHub after agent completion under the workspace contract.
