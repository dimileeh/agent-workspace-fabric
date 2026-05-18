# Review Thread PRRT_kwDOSJAM6s6Cr0AM Plan

## Problem Statement And Scope

Address the unresolved PR review thread on `src/awf/service/workspaces.py`.
The review reports that create idempotency rejects legacy workspace rows whose
stored `task_kind` is `NULL`, even though legacy rows should be treated as
`feature_branch_pr`.

Scope is limited to the workspace create idempotency matcher and focused
regression coverage.

## Requirements Checklist

- Add a regression test showing a replay for a legacy `NULL` task kind matches
  a default `feature_branch_pr` create request.
- Preserve conflict detection for explicit non-default task kinds.
- Keep lifecycle status out of the create idempotency comparison.
- Run focused verification for the changed unit test file and lint the touched
  Python files.

## Implementation Steps

1. Add a focused regression in `tests/unit/service/test_workspace_idempotency.py`
   that sets a loaded workspace's `task_kind` to `None` before invoking
   `workspace_create_payload_matches`.
2. Run the new regression and confirm it fails before the code change.
3. Update `workspace_create_payload_matches` so `None` is normalized to
   `feature_branch_pr` for stored task kind comparison.
4. Run the focused unit test file and lint the touched Python files.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_payload_match_treats_legacy_null_task_kind_as_feature_branch_pr -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py
```

Pass criteria: the new regression fails before implementation, then passes with
the focused idempotency test file and lint.
