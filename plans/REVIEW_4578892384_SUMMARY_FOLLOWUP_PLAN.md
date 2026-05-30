# Review 4578892384 Summary Follow-Up Plan

## Problem Statement And Scope

PR review comment `issue:4578892384` identifies two remaining summary-level gaps:

- The DB-level custom-profile plan artifact overlap test uses two different
  concrete artifact paths, so it would pass even if custom plan artifact
  filtering were disabled.
- `_persist_resolved_profile_snapshot_if_missing()` logs nothing when a
  non-parseable non-null `RETURNING` value forces it to use the runtime profile
  snapshot fallback.

Scope is limited to strengthening the custom artifact overlap regression and
adding observable warning coverage for the executor profile snapshot fallback.
No GitHub comments, pushes, branch changes, broad AWF validation, full coverage
gate, or CI-equivalent command will be run inside this agent phase.

## Requirements Checklist

- Update the custom-profile DB overlap test so the existing and requested
  custom artifact paths genuinely overlap before filtering.
- Keep the custom test focused on non-overlapping real source paths plus one
  overlapping custom plan artifact path.
- Add focused regression coverage that the executor emits a warning before
  falling back to the runtime snapshot for an unparseable `RETURNING` value.
- Avoid logging the raw resolved-profile value or any potentially sensitive
  payload contents.
- Run only focused tests and lint for changed files; leave broad validation to
  AWF/GitHub after agent completion.

## Implementation Steps

1. Update the DB custom artifact overlap test to use an existing
   `docs/alternate/ws_*.md` pattern and requested concrete workspace artifact,
   with a control assertion that the raw owned-path matcher sees an overlap.
2. Add a focused executor snapshot test that monkeypatches the state-ops logger
   and expects a warning on an opaque non-null `RETURNING` value.
3. Run the new executor test before implementation and confirm it fails when
   practical.
4. Add a warning in `_persist_resolved_profile_snapshot_if_missing()` for the
   unparseable non-null `RETURNING` fallback, logging only the workspace id and
   returned value type.
5. Run the targeted DB/executor tests and focused ruff for changed Python files.
6. Record requirement-by-requirement evidence in
   `plans/REVIEW_4578892384_SUMMARY_FOLLOWUP_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_logs_warning_for_unparseable_returning_value -q`
  fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_custom_internal_plan_artifact_overlap_does_not_report_interworkspace_overlap tests/unit/control/test_executor_runtime_profile_snapshot.py::test_runtime_profile_snapshot_logs_warning_for_unparseable_returning_value -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/state_ops.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  passes after implementation.
- Full AWF/GitHub validation is intentionally not run locally; AWF owns broad
  validation, provenance, logs, and merge gating after this agent cycle.
