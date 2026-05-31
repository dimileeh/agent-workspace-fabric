# Review Issue 4585067239 Workflow Scope Needs Human Validation

Plan reference:
`plans/review_issue_4585067239_workflow_scope_needs_human_PLAN.md`

## Requirement Status

- Complete: Mark publish-dependent `fix_committed` inline threads and review
  comments as `needs_human` with the exact workflow-scope permission reason.
  - Evidence: `src/awf/runtime/pr_monitor_runner/fix_cycle.py` tracks
    workflow-scope publish-dependent fix IDs separately and stores
    `needs_human` verdicts via `_sync_needs_human_reason`.
- Complete: Preserve body-hash state so stale-state cleanup does not
  immediately clear the stored `needs_human` verdict.
  - Evidence: The workflow-scope helper overwrites only the verdict and
    needs-human reason, leaving existing body-hash keys intact; focused tests
    assert those keys remain.
- Complete: Preserve push-independent inline `defer` and `false_positive`
  verdicts during workflow-scope push rejection handling.
  - Evidence: Focused tests assert captured-defer and false-positive inline
    verdict state remains addressed after `GITHUB_WORKFLOW_SCOPE_REQUIRED`.
- Complete: Ensure subsequent monitor decisions do not re-enter comment repair
  for workflow-scope-blocked items and can surface the stored reason.
  - Evidence: Focused tests assert `decide()` returns `NotifyHuman` and
    `_notify_human_reason()` returns the stored workflow-scope message.
- Complete: Run only focused local validation for the changed monitor behavior.
  - Evidence: Focused commands listed below. Full AWF/GitHub validation is left
    to AWF after agent completion.

## Verification Evidence

- Expected TDD failure before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k 'workflow_scope_push_failure or workflow_scope_requeue_marks or notify_human_reason'`
  - Result: `4 failed, 3 passed, 20 deselected`
  - Failures showed workflow-scope handling still cleared item state and the
    helper did not accept a stored reason.
- Expected TDD failure before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q -k 'workflow_scope'`
  - Result: `2 failed, 20 deselected`
  - Failures showed outer-loop workflow-scope comment repair still left the
    thread unaddressed.
- Passing focused behavior checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k 'workflow_scope_push_failure or workflow_scope_requeue_marks or notify_human_reason'`
  - Result: `7 passed, 20 deselected`
- Passing focused outer-loop checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q -k 'workflow_scope'`
  - Result: `2 passed, 20 deselected`
- Passing focused lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  - Result: `All checks passed!`
- Passing focused format check after applying `ruff format` to the changed
  Python files:
  - `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  - Result: `3 files already formatted`
- Passing primary affected unit file:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Result: `27 passed`
- Whitespace check:
  - `git diff --check`
  - Result: no output.

Full AWF/GitHub validation was not run locally because the workspace contract
assigns broad validation, provenance, logs, timeouts, and merge gating to AWF
after agent completion.
