# PR270 CI Failures Plan

## Problem Statement And Scope

PR #270 fails the focused CI repro on:

- `tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing`
- `tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs`

The scope is limited to fixing the reported Python CI failures without weakening checks or changing AWF branch/push behavior.

## Requirements Checklist

- Reproduce the reported focused test failures before editing behavior.
- Keep resource saturation metrics scoped to the local workspace routing lane when reporting allocated resources.
- Stop publishing generated plan/validation artifacts as tracked public docs while preserving the canonical plan execution protocol.
- Run the focused pytest repro after the fix.
- Run narrow lint/type/test validation appropriate to touched Python code.
- Commit the fix locally with a conventional commit message and do not push.

## Implementation Steps

1. Inspect the resource saturation aggregation path and the public docs tracking test.
2. Remove tracked generated plan artifacts from the repository index/tree.
3. Update allocated resource aggregation so local metrics do not count reservations belonging to workspaces routed to other worker nodes.
4. Re-run the focused repro and targeted validation.
5. Record validation evidence in `plans/PR270_CI_FAILURES_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs -q`
  - Passes both previously failing tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py -q`
  - Passes the affected test files.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py`
  - Reports no lint failures for touched Python surfaces.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Reports no type errors in AWF source.
