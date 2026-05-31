# Issue 306 Validation

Plan reference: `plans/ISSUE_306_PLAN.md`

## Requirement Status

- Complete: `owned_paths` now flow into diff-classified protected path loading for
  committed, staged, and PR-monitor status diffs.
- Complete: repair prompts render declared `owned_paths`, explicitly state that
  owned protected paths are editable, and reserve protected-file approval
  `NEEDS_HUMAN` for unowned protected files.
- Complete: GitHub workflow-file push rejections caused by missing `workflow`
  scope are detected as `GITHUB_WORKFLOW_SCOPE_REQUIRED`, not generic
  `GIT_PUSH_FAILED`.
- Complete: comment repair push failures for missing workflow scope preserve a
  merge-blocking `needs_human` state with the exact permission reason.
- Complete: regression coverage covers owned protected workflow editability,
  prompt rendering, missing workflow-scope detection, and stored human
  notification reason text.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `src/awf/control/protected_file_diffs.py`
- `src/awf/control/executor/git_methods.py`
- `src/awf/control/executor/quality_methods.py`
- `src/awf/control/executor/execution_flow.py`
- `src/awf/control/executor/execution_validation.py`
- `src/awf/runtime/monitor_prompts.py`
- `src/awf/runtime/pr_monitor_runner/comments.py`
- `src/awf/runtime/pr_monitor_runner/ci_ops.py`
- `src/awf/runtime/pr_monitor_runner/constants.py`
- `src/awf/runtime/pr_monitor_runner/helpers.py`
- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py`
- `tests/unit/runtime/test_monitor_prompts.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/control/executor/git_methods.py src/awf/control/executor/quality_methods.py src/awf/control/executor/execution_flow.py src/awf/control/executor/execution_validation.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/constants.py src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py
uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/control/executor/git_methods.py src/awf/control/executor/quality_methods.py src/awf/control/executor/execution_flow.py src/awf/control/executor/execution_validation.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/constants.py src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py
uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/control/executor/git_methods.py src/awf/control/executor/quality_methods.py src/awf/control/executor/execution_flow.py src/awf/control/executor/execution_validation.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/comments.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/constants.py src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair.py
```

Results:

- Focused touched unit tests: `190 passed`
- Maintainability guard: `9 passed`
- Ruff check: passed
- Ruff format check: passed after formatting touched files
- Mypy: passed

Broad AWF/GitHub validation, full-repository tests, full coverage, OpenAPI drift
checks, and PR creation/description wiring are managed by AWF after this agent
phase. The PR description should include `Closes #306`.
