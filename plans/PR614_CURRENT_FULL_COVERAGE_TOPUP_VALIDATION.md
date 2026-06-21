# PR614 Current Full Coverage Top-Up Validation

Plan reference: `plans/PR614_CURRENT_FULL_COVERAGE_TOPUP_PLAN.md`

## Requirement Status

- Inspect current failed CI run and combined `coverage.xml`: Complete.
  - Evidence: GitHub Actions run `27861705439` failed `python-full-coverage`
    with `79687/80546` combined line+branch opportunities covered
    (`98.93%`, below `99.00%`).
  - Evidence: downloaded artifact `/tmp/awf-pr614-coverage/coverage.xml`
    showed `src/awf/node/git_manager.py` as the largest PR-touched remaining
    gap and additional executor helper command-record/message gaps.
- Add meaningful focused tests for reachable uncovered behavior: Complete.
  - Evidence: added Git manager tests for hooks-path config parsing, include
    repair failure handling, linked-worktree metadata failures, and explicit or
    absent linked git-dir ownership targets.
  - Evidence: added executor helper tests for fallback command counts,
    unreadable artifact handling, coverage metadata rehydration, coverage
    command-failure messaging, healthcheck diagnostics, and validation command
    record ordering/metadata.
- Add a second focused top-up when needed: Complete.
  - Evidence: the node top-up covers roughly 30 opportunities while the exact
    gate was 54 short, so executor validation helper tests were added from the
    same current artifact.
- Keep changes minimal and avoid production refactors: Complete.
  - Evidence: only tests and plan/validation documents changed; no production
    code, workflow, quality-gate, or protected configuration files changed.
- Run focused validation only: Complete.
  - Evidence: focused commands below passed. Full AWF/GitHub validation remains
    owned by AWF after agent completion.

## Focused Command Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::test_hooks_path_config_helpers_normalize_git_config_edges tests/unit/node/test_git_manager.py::TestAgentWorktreeWritable::test_agent_writable_targets_skips_linked_git_dir_when_absent tests/unit/node/test_git_manager.py::TestAgentWorktreeWritable::test_agent_writable_targets_uses_explicit_linked_git_dir tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_repair_ignores_malformed_includeif_probe_line tests/unit/control/test_executor_coverage_gaps.py::test_executor_helper_fallbacks_cover_absent_profile_and_artifact_errors tests/unit/control/test_executor_coverage_gaps.py::test_coverage_result_from_metadata_filters_unexpected_token_types tests/unit/control/test_executor_coverage_gaps.py::test_failure_message_reports_coverage_command_failure_with_baseline tests/unit/control/test_executor_coverage_gaps.py::test_failure_message_reports_healthcheck_metadata tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_006.py::test_validation_run_command_records_delay_healthchecks_until_validate_phase -q`
  - Result: `9 passed in 0.86s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_coverage_gaps.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_006.py tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Result: `All checks passed!`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py --cov=awf.node.git_manager --cov-branch --cov-report=term-missing --cov-fail-under=0 -q`
  - Result: `72 passed in 3.77s`.
  - Targeted coverage evidence: the original current-artifact gaps for
    `node/git_manager.py` are no longer present in the focused missing list.

## Residual Risk

Full combined coverage, shard distribution, and required CI aggregation were
not run locally per the AWF workspace contract. AWF/GitHub CI owns the broad
coverage gate and merge provenance after this agent phase.
