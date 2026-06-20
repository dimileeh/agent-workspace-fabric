# PRRT_kwDOSJAM6s6K0qPk Validation

Plan reference: `PRRT_kwDOSJAM6s6K0qPk_PLAN.md`

## Requirement Status

- Final recovered `HEAD` lookup must not inherit `GIT_OBJECT_DIRECTORY` or
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`: Complete.
- A recovered SHA must be accepted only when the mirror can prove
  `<sha>^{commit}` with `git cat-file -e` under a sanitized object lookup
  environment: Complete.
- Failed final mirror verification must fail closed by returning `None`:
  Complete.
- Existing recovery behavior outside this verification path must remain
  unchanged: Complete for the direct recovery-helper coverage exercised here.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_verifies_final_head_in_mirror -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_during_merge tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_updates_expected_branch_ref tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_verifies_final_head_in_mirror tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_blocks_policy_before_recovery_commit tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_unstages_runtime_paths_without_deletion -q`
  - Passed: `6 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
  - Passed.

Additional observation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q`
  was attempted as a focused file-level check. It failed in five tests that
  monkeypatch `_recover_missing_head_object_from_filesystem`, so they do not
  exercise this thread's final `rev-parse` and mirror `cat-file` change. The
  failures stem from tests feeding porcelain status text such as
  ` M src/foo.py\n` into a `git diff --name-status -z` parser that now expects
  NUL-delimited name-status output. This appears unrelated to
  PRRT_kwDOSJAM6s6K0qPk and was not changed here to keep this fix scoped.

Full AWF/GitHub validation is managed by AWF after agent completion and was
intentionally not run in this workspace phase.
