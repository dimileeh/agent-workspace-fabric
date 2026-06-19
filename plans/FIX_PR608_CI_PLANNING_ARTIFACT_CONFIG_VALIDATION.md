# Fix PR 608 CI Planning Artifact Config Validation

Plan reference: `plans/FIX_PR608_CI_PLANNING_ARTIFACT_CONFIG_PLAN.md`

## Requirement Status

- Reproduce the CI failure with the focused failing test: Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning -q`
  - Before the fix, this reproduced the CI `AttributeError` from
    `_deposit_planning_artifacts_best_effort`.
- Preserve real executor artifact deposit behavior when `compose_projects_root`
  is present: Complete.
  - Existing `test_planning_artifacts_deposit_ordering.py` coverage still
    passes, including the normal deposit path.
- Ensure missing artifact-root configuration is skipped as best-effort instead
  of escaping and changing validation control flow: Complete.
  - Added
    `test_deposit_skips_missing_artifact_root_config_instead_of_raising`.
- Add or update focused regression coverage for the skipped-deposit behavior:
  Complete.
  - Added the focused regression test above and updated the runtime profile
    snapshot ordering assertion to match current validation flow.
- Address the fresh shard-8 line-limit failure: Complete.
  - Trimmed non-executable prose in the two oversized test files; no test bodies
    or assertions were removed.
- Run only targeted verification; leave full AWF/GitHub validation to AWF after
  this agent phase: Complete.
  - Focused commands are listed below. Full AWF/GitHub CI and coverage gates
    were not run locally.
- Commit the fix locally on the current AWF branch without pushing: Complete.
  - Local commit is created after validation evidence is recorded.

## Evidence

Files changed:

- `src/awf/control/executor/planning_artifacts.py`
- `tests/unit/control/test_planning_artifacts_deposit_ordering.py`
- `tests/unit/control/test_executor_runtime_profile_snapshot.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py`
- `plans/FIX_PR608_CI_PLANNING_ARTIFACT_CONFIG_PLAN.md`
- `plans/FIX_PR608_CI_PLANNING_ARTIFACT_CONFIG_VALIDATION.md`

Focused verification run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning -q`
  - Passed: `1 passed in 0.84s`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_artifacts_deposit_ordering.py -q`
  - Passed: `4 passed in 0.74s`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `1 passed in 0.50s`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning tests/unit/control/test_planning_artifacts_deposit_ordering.py -q`
  - Passed: `5 passed in 0.88s`
- `wc -l tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py`
  - `part_002.py`: 1498 lines
  - `part_009.py`: 1492 lines

## Iteration Notes

No remaining planned gaps. Full CI and coverage provenance remain managed by
AWF/GitHub after agent completion.
