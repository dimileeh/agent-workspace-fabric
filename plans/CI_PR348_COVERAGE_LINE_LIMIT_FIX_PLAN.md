# PR #348 CI Coverage And Line-Limit Fix Plan

## Problem Statement And Scope

PR #348 currently fails the `python-full-coverage` GitHub Actions job. The
failed run for commit `72f5c66f0009f86d9914f93305d38938726cd218` shows:

- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` exceeds the
  first-party file limit at 1653 lines.
- Combined line+branch coverage is `98.99%`, below the required `99%`.

Scope is limited to test decomposition and focused regression coverage for the
PR-monitor validation/push paths touched by this PR. Protected workflows,
quality gates, and CI configuration are not edited.

## Requirements Checklist

- [ ] Keep every first-party code file at or below the 1500-line limit.
- [ ] Preserve the 99% coverage gate without weakening CI or coverage config.
- [ ] Cover the uncovered pre-push validation retry-loop branch.
- [ ] Cover the best-effort sync-base staleness refresh exception path.
- [ ] Cover the monitor handoff factory-returned-`None` path that reports an
      unavailable PR monitor after setup succeeds.
- [ ] Run only focused local verification; AWF/GitHub owns broad coverage and
      CI validation after agent completion.
- [ ] Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Confirm the focused line-limit failure locally.
2. Split the tail-end PR-monitor repair-flow tests out of
   `test_pr_monitor_pre_push_validation.py` into a smaller companion test file.
3. Refine or test the pre-push validation retry loop so the terminal failing
   retry path has explicit coverage without changing behavior.
4. Add a focused unit test for sync-base staleness refresh logging when the
   best-effort database refresh raises.
5. Add a focused unit test for the monitor handoff path where a configured
   factory returns no monitor after setup succeeds.
6. Run focused pytest and Ruff checks for the touched tests/modules, plus the
   line-limit guard.
7. Save validation evidence in
   `plans/CI_PR348_COVERAGE_LINE_LIMIT_FIX_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_edges.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_monitor_factory_none_marks_unavailable_after_setup -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_edges.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

All focused commands must pass. Full repository coverage, whole-repository
pytest, frontend builds, and CI-equivalent gates remain managed by AWF/GitHub
after this fix cycle.
