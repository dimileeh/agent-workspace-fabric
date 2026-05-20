# CI 270 Fix Validation

Plan reference: `plans/CI_270_FIX_PLAN.md`

## Requirement Status

- Do not switch branches, push, rebase, or force-push: Complete.
  - Stayed on `feature-sync/ws_679f023a3f324df981975971`; no push/rebase/switch
    commands were run.
- Run the AWF-provided focused repro before broader validation: Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs -q`
    passed with `2 passed in 1.64s`.
- Treat CI failures as real bugs without weakening checks: Complete.
  - The local branch already contains `fix(ci): docs public status - generated
    plan artifacts were tracked` and `fix(ci): metrics capacity - allocated
    totals counted sibling workspace reservations`; no tests were skipped or
    weakened.
- Keep generated plan artifacts local-only: Complete.
  - Existing `.gitignore` rules ignore generated `plans/*` artifacts while
    preserving `plans/PLAN_EXECUTION_PROTOCOL.md`.
- Verify metrics allocation scoping and public docs status: Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py -q`
    passed with `28 passed in 11.60s`.
- Commit any new code/test fixes if required: Complete.
  - No new patch was required; the relevant fixes are already committed locally
    as the top two commits in the workspace.

## Additional Evidence

- `git ls-files docs/awf-plans plans` listed only:
  - `docs/awf-plans/README.md`
  - `plans/PLAN_EXECUTION_PROTOCOL.md`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_resource_reservation_totals.py tests/unit/service/test_metrics.py -q`
  passed with `91 passed in 57.14s`.

## Gaps

None found. The reported CI failures are covered by existing local commits and
the focused plus adjacent validation commands pass in this workspace.
