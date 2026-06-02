# Fix PR 328 CI Validation

Plan reference: `plans/fix_pr328_ci_PLAN.md`

## Requirement Status

- Complete: Preserve grouped terminal runtime release failures.
  - `src/awf/control/worker/cleanup.py` now raises grouped release failures
    before any planning-scope retry resume pass.
  - Evidence: focused runtime-release pytest command passed.

- Complete: Avoid opening the planning-scope retry session when the configured
  terminal runtime release scan limit is empty.
  - `src/awf/control/worker/cleanup.py` short-circuits `limit <= 0`.
  - `tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py`
    adds `test_release_terminal_runtime_resources_skips_empty_limit`.

- Complete: Keep every first-party source/test file at or under 1,500 lines.
  - Moved one worker capacity test into
    `tests/unit/control/test_worker_parts/test_worker_part_043.py`.
  - Moved legacy host-port tests into
    `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py`.
  - Moved the legacy workspace retry test into
    `tests/unit/service/test_workspace_retry_legacy.py`.

- Complete: Run only focused repro/validation commands locally.
  - No full unit suite, full coverage gate, frontend build, or CI-equivalent
    validation was run locally.
  - Full AWF/GitHub validation is managed by AWF after agent completion.

- Complete: Commit the local fix on the current AWF branch without pushing.
  - The final step for this fix cycle is one local conventional commit on the
    current AWF branch; AWF will handle push and PR updates after agent
    completion.

## Evidence

- Initial repro:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result before fix: 2 failed, 1 passed.

- Runtime release fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures -q`
  - Result: 1 passed.

- Original focused repro after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: 3 passed.

- Moved split tests:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_043.py::TestRunOnceCapacityDecisionsPart043::test_requested_capacity_gate_scans_only_workspaces_for_worker_node tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py tests/unit/service/test_workspace_retry_legacy.py -q`
  - Result after helper fix: 4 passed.

- Final focused repro plus empty-limit regression:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_release_terminal_runtime_resources_skips_empty_limit tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: 4 passed.

- Focused ruff:
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py tests/unit/control/test_worker_parts/test_worker_part_003.py tests/unit/control/test_worker_parts/test_worker_part_043.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py tests/unit/service/test_workspace_retry.py tests/unit/service/test_workspace_retry_legacy.py`
  - Result: passed.
