# Protected Scope Committed Repair Prompt Validation

Plan reference: `plans/PROTECTED_SCOPE_COMMITTED_REPAIR_PROMPT_PLAN.md`

## Requirement Status

- Add a regression test proving the committed-diff repair call receives history-level guidance: Complete.
  Evidence: `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` now asserts the committed repair adapter prompt says protected edits are already committed locally, must be removed from branch history relative to the PR head, and can use a reverting commit.

- Preserve the existing dirty-worktree protected-scope repair behavior and wording: Complete.
  Evidence: `_protected_scope_repair_prompt` keeps the original worktree-oriented wording; only its shared path/owned-path rendering was factored into `_protected_scope_prompt_sections`.

- Route only the committed pre-push protected-scope repair path to the committed-diff prompt variant: Complete.
  Evidence: `_repair_protected_scope_commits_before_push` now calls `_protected_scope_committed_repair_prompt`; `_repair_protected_scope_changes_before_commit` still calls `_protected_scope_repair_prompt`.

- Keep changes scoped to monitor-runner behavior and tests: Complete.
  Evidence: changed files are limited to `src/awf/runtime/pr_monitor_runner.py`, `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`, and this plan/validation pair.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry -q
```

Result before implementation: failed because the committed repair prompt did not contain `already committed locally`.

Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
```

Result: `128 passed in 125.35s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py
```

Result: passed.

## Gaps

No known gaps.
