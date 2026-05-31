# Review PRRT_kwDOSJAM6s6F6zHQ Pre-Commit Staged Paths Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6F6zHQ_precommit_staged_paths_PLAN.md`

## Requirement Status

- Add a regression test showing an unaffected staged operation path does not
  block a deterministic pre-commit autofix retry: Complete.
- Keep rejecting retry attempts when a worktree-modified path is outside the
  hook repair set: Complete.
- Keep retry scope bounded to paths that were dirty at the start of the monitor
  commit operation: Complete.
- Restage only the hook-modified repair paths needed for the retry: Complete.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/commit_autofix.py`
- `tests/unit/runtime/test_pr_monitor_commit_autofix.py`
- `plans/review_PRRT_kwDOSJAM6s6F6zHQ_precommit_staged_paths_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6F6zHQ_precommit_staged_paths_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py -q
```

Result before implementation: failed on
`test_monitor_precommit_autofix_retry_allows_unaffected_staged_paths`, confirming
the reviewer-reported skip.

Result after implementation: passed, `4 passed in 0.71s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q -k autofix
```

Result after implementation: passed, `4 passed, 19 deselected in 4.70s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/commit_autofix.py tests/unit/runtime/test_pr_monitor_commit_autofix.py
```

Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/commit_autofix.py
```

Result after implementation: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
