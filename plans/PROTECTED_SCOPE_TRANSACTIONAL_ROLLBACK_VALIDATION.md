# Protected-Scope Transactional Rollback Validation

## Summary

Implemented the protected-scope repair rollback plan. PR monitor CI and comment
repairs now treat committed protected-scope edits as a transactional failure:
AWF records the protected paths, rolls the local branch back to the operation
start SHA, cleans local leftovers, returns a protected-scope blocked result, and
does not push partial collateral files.

After review feedback on PR #284, tightened the rollback baseline further:
operation start is now the local worktree `HEAD` captured before the repair
agent mutates the workspace, not the remote PR head passed in the status
payload. The rollback path also reports that local start SHA in structured audit
columns, uses NUL-delimited committed-diff parsing for odd filenames, and no
longer keeps the dead ownership-repair exception wrapper around deterministic
rollback.

After the follow-up Greptile P1 on commit `c147d2bd`, the diagnostic reverted
path collection is now best-effort for malformed `git diff --name-status -z`
output. A parse error is logged, but it cannot prevent the safety-critical
`git reset --hard <operation-start>` and `git clean -fd` rollback from running.

## Coverage Added

- CI repair regression: a local repair commit touching `.github/workflows/ci.yml`
  plus collateral files is rolled back to the captured local operation start
  head, the workspace fails with protected-scope evidence, and no push happens.
- Direct rollback regression: committed protected-scope repair deltas are reset
  without invoking a second agent repair.
- Comment repair regression: addressed-state is cleared when protected rollback
  blocks publication, so feedback remains actionable.
- Prompt regressions: CI, inline thread, and review-level repair prompts include
  generic protected workflow/quality-gate/config deferral guidance without
  language-specific wording.
- Review follow-up regressions: rollback delta path collection now uses
  NUL-delimited name-status output, preserving filenames containing newlines;
  fix-cycle rollback audit events carry a structured source head SHA.
- Parse-failure rollback regression: malformed diagnostic name-status output is
  logged and ignored while rollback continues, with dirty/untracked paths still
  reported when available.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Initial result before review follow-up: `213 passed in 139.18s`
  - Review follow-up result: `213 passed in 151.34s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k 'protected_scope_commit_repair or execute_ci_fix_rolls_back or fix_cycle_rolls_back_protected_scope_delta or protected_file_changes_generically'`
  - Result after formatting: `7 passed, 206 deselected in 5.45s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_treats_transient_settle_poll_as_retryable tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_resolve_thread_transient_failure_requeues_thread_safely tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_reraises_non_transient_settle_poll_error tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_zero_passes_still_runs_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_execute_report_ci_failure_dispatches_fix_and_increments_iteration tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_execute_report_ci_failure_push_failure_records_failed_audit tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_monitor_adapter_cleanup_failure_terminates_without_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_monitor_comment_cleanup_failure_terminates_without_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_monitor_comment_diff_baseline_unavailable_terminates_with_diff_reason tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_addresses_new_review_burst_before_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_readdresses_thread_when_history_changes_before_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_fix_cycle_does_not_readdress_thread_for_agent_resolution_reply tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_commits_and_pushes_even_if_agent_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocking_supply_chain_finding_is_not_committed_or_pushed tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_protected_scope_repair_ownership_repair_failure_returns_failed_push tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_blocks_committed_protected_quality_gate_edits_after_retry -q`
  - Result: `17 passed in 18.57s`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k 'protected_scope_delta'`
  - Result: `2 passed, 168 deselected in 2.28s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_prompts.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py`
  - Result: `Success: no issues found in 2 source files`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`
  - Result: `Success: no issues found in 1 source file`

## Notes

- Full coverage and whole-repo validation remain delegated to GitHub CI/AWF.
- Existing local Gemini ripgrep changes in `docker/agent-runtime.Dockerfile` and
  `tests/unit/test_agent_runtime_dockerfile.py` were preserved and not folded
  into this protected-scope rollback change.
