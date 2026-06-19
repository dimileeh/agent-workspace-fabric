# Fix PR 608 CI Planning Artifact Config Plan

## Problem Statement And Scope

PR #608 CI failed in `python-coverage-shards (3)` because
`test_validation_cycle_syncs_profile_before_command_planning` exercises
validation with a lightweight executor test double whose `_config` lacks
`compose_projects_root`. `_deposit_planning_artifacts_best_effort` is documented
as non-fatal, but it dereferences `_config.compose_projects_root` before the
best-effort artifact copy boundary and raises `AttributeError`.

Scope is limited to making planning artifact deposit non-fatal when the executor
does not have an artifact-root config and covering that behavior with focused
tests.

## Requirements Checklist

- Reproduce the CI failure with the focused failing test.
- Preserve real executor artifact deposit behavior when `compose_projects_root`
  is present.
- Ensure missing artifact-root configuration is skipped as best-effort instead
  of escaping and changing validation control flow.
- Add or update focused regression coverage for the skipped-deposit behavior.
- Run only targeted verification; leave full AWF/GitHub validation to AWF after
  this agent phase.
- Commit the fix locally on the current AWF branch without pushing.

## Implementation Steps

1. Inspect the failing CI log and confirm the failing test locally.
2. Update `_deposit_planning_artifacts_best_effort` to check for
   `_config.compose_projects_root` before calling
   `deposit_workspace_planning_artifacts`.
3. Log a warning and return when the artifact root is unavailable, preserving
   the helper's best-effort contract.
4. Add a focused unit test asserting missing config does not raise and does not
   attempt a deposit.
5. Run the exact failed test and the focused planning-artifact deposit tests.
6. Write validation evidence to
   `plans/FIX_PR608_CI_PLANNING_ARTIFACT_CONFIG_VALIDATION.md`.
7. Commit the scoped changes locally.

## Assumptions/Changes

- After the initial focused fix, the fresh GitHub run for the current PR head
  failed `python-coverage-shards (8)` on
  `test_first_party_code_files_stay_under_line_limit`.
- The additional shard-8 failure is in scope for this CI fix cycle. The fix is
  test-only prose trimming in the two oversized test files while preserving all
  assertions and test bodies.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning -q`
  - Passes; the prior `AttributeError` is gone.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_artifacts_deposit_ordering.py -q`
  - Passes; existing and new deposit-ordering/best-effort behavior remains
    covered.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes; no first-party file exceeds the 1,500-line guard.

Full AWF/GitHub CI and coverage gates are intentionally not run locally per the
workspace contract; AWF owns broad validation after this fix cycle.
