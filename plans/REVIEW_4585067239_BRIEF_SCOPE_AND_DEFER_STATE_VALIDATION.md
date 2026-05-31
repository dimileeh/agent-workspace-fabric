# Review 4585067239 Brief Scope and Defer State Validation

Plan reference: `plans/REVIEW_4585067239_BRIEF_SCOPE_AND_DEFER_STATE_PLAN.md`

## Requirement Status

- Complete: Added a regression for terse GitHub hook output that says workflow
  permissions are required without naming `.github/workflows/`.
- Complete: Preserved the existing false-positive guard for unrelated
  workflow-scope text embedded in a generic remote rejection.
- Complete: Added a two-cycle regression showing a captured `defer` thread is
  not re-addressed after a workflow-scope push failure followed by an unrelated
  generic push failure in a later fix cycle.
- Complete: Re-ran nearby workflow-scope parser and git-push result tests to
  confirm existing wording variants and push-independent defer preservation
  still pass.
- Complete: Ran only focused checks. Full AWF/GitHub validation, coverage
  gates, frontend builds, and CI-equivalent commands remain owned by AWF after
  agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `plans/REVIEW_4585067239_BRIEF_SCOPE_AND_DEFER_STATE_PLAN.md`
- `plans/REVIEW_4585067239_BRIEF_SCOPE_AND_DEFER_STATE_VALIDATION.md`

Focused red check before implementation:

```bash
uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_handles_terse_hook_output_without_workflow_path tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_ignores_remote_rejected_without_workflow_file_context tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_later_generic_push_failure_keeps_workflow_scope_preserved_defer_state
```

Result: failed as expected for the new terse hook-output detector regression;
the existing false-positive guard and two-cycle defer-state regression passed.

Focused green checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_handles_terse_hook_output_without_workflow_path tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_ignores_remote_rejected_without_workflow_file_context tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_later_generic_push_failure_keeps_workflow_scope_preserved_defer_state
```

Result: passed, `3 passed`.

```bash
uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_handles_alternate_github_wording tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_block_ignores_unrelated_workflow_output tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_git_push_result_maps_github_workflow_scope_rejection tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_git_push_result_detects_workflow_scope_rejection_across_streams tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_requeues_captured_defer_thread_state
```

Result: passed, `15 passed`. The five selectors collect 15 tests because
`test_workflow_scope_push_block_handles_alternate_github_wording` is
parametrized into 11 cases and the other four selectors collect one test each.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py
```

Result: passed, `All checks passed!`.
