# PR614 Repair Start HEAD CI Fix Validation

Plan reference: `plans/PR614_REPAIR_START_HEAD_CI_FIX_PLAN.md`

## Requirement Status

- Diagnose the exact guard short-circuiting shard 1, shard 5, and shard 6 tests: Complete.
  The failed CI tests unexpectedly returned `REPAIR_START_HEAD_UNAVAILABLE`
  before monitor fix-cycle behavior ran because no-mirror unit-test worktrees
  could not pass the new fallback start-head validation.
- Preserve the new protected-scope behavior: Complete. Mirror-backed fallbacks
  still validate the commit object with `git cat-file`; no-mirror fallbacks only
  proceed when the existing head-object guard reports a valid anchor, and still
  return `REPAIR_START_HEAD_UNAVAILABLE` when that guard fails.
- Restore existing PR monitor fix-cycle behavior for valid fallback heads:
  Complete. Representative shard 1, shard 5, and shard 6 failures now reach
  their intended monitor paths instead of short-circuiting at repair start.
- Add/update focused regression coverage: Complete. Added explicit no-mirror
  fallback acceptance and rejection tests in the remote-repair edge suite.
- Run focused checks only: Complete. Full AWF/GitHub validation and coverage
  gates were not run locally; AWF owns them after agent completion.
- Avoid protected workflow/quality-gate config edits: Complete. No protected
  workflow or gate configuration was edited.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/remote_repair.py`.
- Changed `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`.
- Added this validation document and `plans/PR614_REPAIR_START_HEAD_CI_FIX_PLAN.md`.

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_run_fix_cycle_resolves_task_tag_once_for_multiple_items -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_accepts_mocked_no_mirror_fallback tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_rejects_no_mirror_fallback_when_guard_fails tests/unit/runtime/test_pr_monitor_task_tag_threading.py::test_run_fix_cycle_resolves_task_tag_once_for_multiple_items tests/unit/runtime/test_monitor_action_logging.py::TestMonitorActionLogging::test_address_comments_action_emits_log_line tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_generic_push_failure_preserves_review_comment_needs_human_after_later_pass -q
```

Result: `5 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py tests/unit/runtime/test_pr_monitor_fix_cycle_outdated_preserve.py tests/unit/runtime/test_pr_monitor_manual_merge.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_014.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_021.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_008.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_009.py -q
```

Result: `253 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q
```

Result: `28 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py -q
```

Result: `76 passed`.

## Remaining Gaps

None for the scoped fix. Full CI, shard coverage, and merge-gate provenance are
left to AWF/GitHub after this agent phase per workspace contract. The inspected
`python-full-coverage` failure was an aggregate early failure because coverage
shards failed; no independent local full-coverage gate was run.
