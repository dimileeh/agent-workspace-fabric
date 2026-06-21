# PR614 Full Coverage Top-Up Validation

## Plan Check

- Added behavior-level tests for mirror hooks repair `OSError` paths in executor
  startup/cleanup handling.
- Added focused PR monitor remote-repair tests for recovered dirty-worktree
  protected-scope lookup errors, recovery cleanup failures, missing recovery
  heads, and runtime-memory staging filters.
- Added focused pre-push validation fix-pass tests for post-agent mirror repair
  failure, cleanup failure reason precedence, missing recovery anchors, and
  commit-exception rollback failure.
- Added small regression tests for verdict-agent terminal repair errors,
  protected refspec parsing, and mirror worktree registration fail-closed cases.
- Did not edit workflow, threshold, protected config, or broad validation files.

## Evidence

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_mirror_hooks_path.py::test_repair_mirror_hooks_path_or_mark_failed_marks_failed_on_oserror \
  tests/unit/control/test_executor_mirror_hooks_path.py::test_repair_mirror_hooks_path_after_agent_cleanup_failure_logs_oserror \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_027.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_028.py \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_005.py \
  tests/unit/control/test_protected_file_diffs.py::test_git_refspec_missing_path_rejects_malformed_refspecs \
  tests/unit/control/test_protected_file_diffs.py::test_git_z_listing_treats_malformed_record_as_present \
  tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestMirrorHasRegisteredHooksPath -q
```

Result: `27 passed in 7.11s`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check \
  tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_005.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_027.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_028.py \
  tests/unit/control/test_executor_mirror_hooks_path.py \
  tests/unit/control/test_protected_file_diffs.py \
  tests/unit/node/test_git_manager_mirror_hooks_repair.py
```

Result: `All checks passed!`.

Focused maintainability guard:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result: `1 passed in 0.47s`.

Line counts for touched test files:

```text
398 tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_005.py
417 tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_027.py
82 tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_028.py
1092 tests/unit/control/test_executor_mirror_hooks_path.py
384 tests/unit/control/test_protected_file_diffs.py
479 tests/unit/node/test_git_manager_mirror_hooks_repair.py
```

## Notes

The CI artifact from run `27848776769` showed `python-full-coverage` failing at
`98.94%` against `99.00%`, while all shard jobs passed. The added tests target
reachable missing branches from that artifact in PR-touched modules rather than
changing thresholds or excluding live code.

A temporary local coverage diagnostic using `COVERAGE_FILE=/tmp/awf-targeted.coverage`
segfaulted inside `asyncpg` during pytest collection. The ordinary focused pytest
commands above pass. Full combined coverage validation remains owned by
AWF/GitHub after this agent phase, per the workspace contract.
