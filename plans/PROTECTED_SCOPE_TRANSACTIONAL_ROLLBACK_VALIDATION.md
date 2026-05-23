# Protected-Scope Transactional Rollback Validation

## Summary

Implemented the protected-scope repair rollback plan. PR monitor CI and comment
repairs now treat committed protected-scope edits as a transactional failure:
AWF records the protected paths, rolls the local branch back to the operation
start SHA, cleans local leftovers, returns a protected-scope blocked result, and
does not push partial collateral files.

## Coverage Added

- CI repair regression: a local repair commit touching `.github/workflows/ci.yml`
  plus collateral files is rolled back to the PR head, the workspace fails with
  protected-scope evidence, and no push happens.
- Direct rollback regression: committed protected-scope repair deltas are reset
  without invoking a second agent repair.
- Comment repair regression: addressed-state is cleared when protected rollback
  blocks publication, so feedback remains actionable.
- Prompt regressions: CI, inline thread, and review-level repair prompts include
  generic protected workflow/quality-gate/config deferral guidance without
  language-specific wording.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Result: `213 passed in 139.18s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k 'protected_scope_commit_repair or execute_ci_fix_rolls_back or fix_cycle_rolls_back_protected_scope_delta or protected_file_changes_generically'`
  - Result after formatting: `7 passed, 206 deselected in 5.45s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_prompts.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py`
  - Result: `Success: no issues found in 2 source files`

## Notes

- Full coverage and whole-repo validation remain delegated to GitHub CI/AWF.
- Existing local Gemini ripgrep changes in `docker/agent-runtime.Dockerfile` and
  `tests/unit/test_agent_runtime_dockerfile.py` were preserved and not folded
  into this protected-scope rollback change.
