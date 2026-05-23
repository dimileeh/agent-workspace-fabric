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
After the follow-up CodeRabbit quick-win on commit `01f7b7ae`, those
best-effort path collection failures are also preserved in the rollback
evidence payload, so operators can distinguish "no reverted paths" from
"diagnostic path collection failed but rollback still ran."
After the follow-up CodeRabbit review on commit `20bebba0`, monitor repair
operations now refuse to start if the worktree is already dirty before the
agent runs, and rollback evidence records whether `git clean -fd` actually ran.
That keeps transactional rollback scoped to the current repair operation and
avoids reporting a synthetic clean success after a failed reset.

PR #284 then exposed a protected-workflow CI blocker: `astral-sh/setup-uv@v4`
was using its default `${{ github.token }}` while resolving the broad
`0.5.x` version range, and GitHub Actions failed before project code ran with
`Bad credentials`. Because the live AWF worker does not yet have this
transactional protected-scope guard, this workflow repair was applied manually:
all CI `setup-uv` steps now pin `uv` to `0.5.31` and pass an empty
`github-token` so the action avoids the bad default token path.

After the next review pass on PR #284, the rollback and repair-start behavior
was tightened again: repair-start dirty worktrees and repair status-inspection
failures now terminate the monitor instead of retrying every poll, rollback
cleanup uses literal pathspecs for collected repair-created untracked paths
instead of global `git clean -fd`, and malformed name-status rollback evidence
falls back to `git diff --name-only -z` before reporting partial evidence.

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
  reported when available. The rollback evidence now includes a structured
  `reverted_path_collection_errors` entry for the parse failure.
- Dirty-start/rollback-evidence regressions: CI repair refuses to invoke the
  agent when pre-existing dirty worktree state is detected; failed rollback
  reset evidence omits unattempted `clean_*` fields.
- CI workflow regression: all `setup-uv` steps pin an explicit uv version and
  avoid the action's default GitHub token release lookup.
- Review follow-up regressions: dirty/status repair-start failures are terminal
  for CI/comment repair actions; protected-scope rollback cleanup only targets
  collected untracked repair leftovers; malformed committed-diff name-status
  output falls back to name-only path collection before reporting partial
  evidence; failed reset evidence omits unattempted clean metadata.

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
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k 'pre_existing_dirty_worktree or protected_scope_delta or failed_reset_omits or ci_fix_blocks_committed_protected_quality_gate_edits_after_retry or ci_fix_rolls_back_instead or ci_fix_rolls_back_before_protected_revert_baseline_fetch or execute_ci_fix_rolls_back_whole_delta or ci_fix_blocking_supply_chain or ci_fix_commits_and_pushes_even_if_agent_fails'`
  - Result: `10 passed, 162 deselected in 11.91s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_prompts.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py src/awf/runtime/monitor_prompts.py`
  - Result: `Success: no issues found in 2 source files`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`
  - Result: `Success: no issues found in 1 source file`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q -k 'setup_uv or release_artifacts'`
  - Result: `2 passed, 12 deselected in 0.20s`
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_ci_workflow_full_coverage.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/test_ci_workflow_full_coverage.py`
  - Result: `1 file already formatted`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k 'pre_existing_dirty_worktree or repair_start_failures_are_terminal or failed_reset_omits or protected_scope_delta or protected_scope_commit_repair_rolls_back_delta_without_agent_or_push or execute_ci_fix_rolls_back_whole_delta_when_local_commit_touches_protected_scope'`
  - Initial review follow-up result: `9 passed, 166 deselected in 12.06s`
  - Result after formatting: `9 passed, 166 deselected in 16.36s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Result: `All checks passed`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - Result: `2 files already formatted`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`
  - Result: `Success: no issues found in 1 source file`

## Notes

- Full coverage and whole-repo validation remain delegated to GitHub CI/AWF.
- Existing local Gemini ripgrep changes in `docker/agent-runtime.Dockerfile` and
  `tests/unit/test_agent_runtime_dockerfile.py` were preserved and not folded
  into this protected-scope rollback change.
