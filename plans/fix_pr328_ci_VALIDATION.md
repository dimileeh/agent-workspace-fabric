# Fix PR 328 CI Validation

Plan reference: `plans/fix_pr328_ci_PLAN.md`

## Requirement Status

- Complete: Preserve grouped terminal runtime release failures.
  - `src/awf/control/worker/cleanup.py` still runs the planning-scope retry
    safety scan after candidate release errors, but logs a secondary safety-scan
    failure instead of appending it to the candidate release exception group.

- Complete: Update service worker unit-test stubbing for current forge wiring.
  - `tests/unit/service/test_worker.py` no longer patches the stale
    `awf.service.worker.GitHubClient` implementation detail.

- Complete: Keep every first-party source/test file at or under 1,500 lines.
  - Moved trailing terminal-runtime worker tests into
    `tests/unit/control/test_worker_parts/test_worker_part_044.py`.
  - Moved legacy host-port collision tests into
    `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py`.
  - Moved the planning-scope retry fallback-model test into
    `tests/unit/service/test_workspace_retry_planning_scope.py`.
  - Moved the legacy-hostname retry port gate test into
    `tests/unit/service/test_workspace_retry_port_legacy.py`.

- Complete: Run only focused repro/validation commands locally.
  - No full unit suite, full coverage gate, frontend build, or CI-equivalent
    validation was run locally.
  - Full AWF/GitHub validation is managed by AWF after agent completion.

- Complete: Commit the local fix on the current AWF branch without pushing.
  - This fix cycle is captured in one local conventional commit; AWF will handle
    push and PR update after agent completion.

## Evidence

- Initial focused repro:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result before fix: 3 failed, 1 passed.

- Focused CI repro after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: 4 passed.

- Moved split tests:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_044.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py tests/unit/service/test_workspace_retry_planning_scope.py tests/unit/service/test_workspace_retry_port_legacy.py -q`
  - Result: 14 passed.

- Focused ruff:
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py tests/unit/control/test_worker_parts/test_worker_part_042.py tests/unit/control/test_worker_parts/test_worker_part_044.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_legacy.py tests/unit/service/test_worker.py tests/unit/service/test_workspace_retry.py tests/unit/service/test_workspace_retry_planning_scope.py tests/unit/service/test_workspace_retry_port.py tests/unit/service/test_workspace_retry_port_legacy.py`
  - Result: passed.

- Final focused repro after import cleanup:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_terminal_runtime_release_groups_multiple_candidate_failures tests/unit/service/test_worker.py::test_build_worker_runtime_defaults_unset_service_node_id_to_local tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: 4 passed.

## Remaining Gaps

None for the focused CI failure. Broad AWF/GitHub validation remains owned by
AWF after this agent phase.
