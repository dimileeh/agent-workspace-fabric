# PR 289 Coverage And Maintainability Validation

Plan reference: `plans/PR_289_COVERAGE_MAINTAINABILITY_PLAN.md`

## Requirement Status

- Keep all work on the current AWF branch: Complete.
- Do not edit protected CI/workflow/quality-gate configuration: Complete.
- Split oversized first-party files below the 1,500 line limit without changing
  behavior: Complete.
- Add focused tests for the companion schema branches missed by coverage:
  Complete.
- Run only targeted local checks for changed behavior and the failing
  maintainability guard: Complete.
- Record validation evidence in this validation document: Complete.
- Commit the fix locally with a conventional commit message: Complete.

## Files Changed

- `src/awf/service/gc.py`
- `src/awf/service/gc_worktrees.py`
- `tests/unit/service/test_gc_more2.py`
- `tests/unit/service/test_gc_worktree_remover.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_003.py`
- `tests/unit/api/test_schema_coverage_edges.py`

## Evidence

- `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/service/gc_worktrees.py tests/unit/service/test_gc_more2.py tests/unit/service/test_gc_worktree_remover.py tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_003.py tests/unit/api/test_schema_coverage_edges.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py tests/unit/service/test_gc_worktree_remover.py -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_recovery_summary_uses_inactive_operator_recovery_operation tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_003.py::test_recovery_summary_bounds_json_payload_from_previous_recovery_event -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py::TestFailureHandling::test_legacy_allowlist_profile_marks_resolution_failed_before_compose_up tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestFailureHandlingEdges -q`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/gc.py src/awf/service/gc_worktrees.py`
  passed.
- `git diff --check` passed.

## Line Limit Evidence

- `src/awf/service/gc.py`: 1,470 lines.
- `tests/unit/service/test_gc_more2.py`: 1,319 lines.
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py`: 1,483 lines.
- `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`:
  1,462 lines.

Full AWF/GitHub validation, full coverage, and CI-equivalent checks were not run
locally per the AWF workspace contract; AWF owns those after agent completion.
