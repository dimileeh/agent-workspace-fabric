# PRRT_kwDOSJAM6s6KyDg Plan

## Problem Statement and Scope

The unresolved review thread reports that `_run_post_validation_conformance_check`
directly reads `self._config.compose_projects_root.parent` when depositing
satisfied planning artifacts. Executor test doubles or reduced configs without
`compose_projects_root` can raise `AttributeError` after conformance has already
passed, even though artifact deposit is intended to be best-effort.

Scope is limited to `src/awf/control/executor/planning_conformance.py`, focused
regression coverage, and this plan/validation evidence.

## Requirements Checklist

- [ ] Verify the missing artifact-root claim against current code.
- [ ] Add a focused failing regression for a satisfied post-validation
      conformance check with no `compose_projects_root`.
- [ ] Guard satisfied-report artifact deposits so missing artifact root logs and
      skips without failing the conformance check.
- [ ] Preserve current artifact deposit behavior when `compose_projects_root` is
      configured.
- [ ] Run only focused checks for the touched behavior/files; broad AWF/GitHub
      validation remains managed after agent completion.

## Implementation Steps

1. Add a targeted unit test in the post-validation conformance edge suite.
2. Add a small helper in `planning_conformance.py` to resolve the served
   artifact work directory from executor config.
3. Use that helper before both satisfied-report deposit branches, skipping the
   best-effort deposit when the root is unavailable.
4. Run the new test, nearby deposit-path tests, and focused lint on changed
   files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_missing_artifact_root_skips_deposit -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_uses_fresh_on_disk_report_and_skips_rewrite tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_stale_report_with_failed_rewrite_uses_in_memory_deposit tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_missing_artifact_root_skips_deposit -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_conformance.py tests/unit/control/test_planning_ops_branch_edges.py`

Pass criteria: targeted tests pass and focused lint reports no issues.
