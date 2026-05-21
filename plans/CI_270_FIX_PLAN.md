# CI 270 Fix Plan

## Problem Statement And Scope

PR #270 reported a failing GitHub Actions CI run in the `python-full-coverage`
job. The quoted failures are:

- `tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing`
- `tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs`

The current AWF workspace already contains local, unpushed commits that appear
to address both failures. This pass will verify those fixes and make an
additional scoped patch only if the failures still reproduce locally.

## Requirements Checklist

- Do not switch branches, push, rebase, or force-push.
- Run the AWF-provided focused repro before broader validation.
- Treat the CI failures as real bugs; do not disable, skip, or weaken tests.
- Keep generated plan artifacts local-only because the public docs status test
  explicitly asserts that generated `plans/*` artifacts are not tracked.
- Verify the metrics allocation scoping behavior and the tracked-public-docs
  invariant with focused commands.
- Commit any new code/test fixes locally if an additional patch is required.

## Implementation Steps

1. Confirm the current branch and worktree state.
2. Run the provided focused repro command.
3. Inspect the local commits that already target the reported failures.
4. Validate that only public plan documentation is tracked.
5. Run the narrow test files covering metrics capacity and public docs status.
6. If any failure remains, patch the smallest relevant production/test surface
   and commit it with a conventional `fix(ci): ...` message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs -q`
  - Passes both reported node IDs.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py -q`
  - Passes the directly related test files.
- `git ls-files docs/awf-plans plans`
  - Lists only `docs/awf-plans/README.md` and
    `plans/PLAN_EXECUTION_PROTOCOL.md`.
