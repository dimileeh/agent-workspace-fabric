# Metrics Capacity Hot Path Review Plan

## Problem Statement and Scope

Address PR review comment `issue:4495131102` for the resource saturation metrics hot path. The scope is limited to `src/awf/service/metrics.py`, the repository helper names it imports, and focused regression coverage.

## Requirements Checklist

- Stop importing underscore-prefixed repository helpers from `metrics.py`; use public repository helper names for scheduler ordering and dialect resolution.
- Avoid loading every matching `Workspace.resolved_profile` JSON blob in `_defaulted_dind_slots_for_session`; aggregate default DinD slot counts in SQL.
- Pre-filter `_capacity_queue_candidates` reservation ranking to requested workspaces in the local node scope before computing the latest active reservation window.
- Preserve existing capacity scheduling semantics, especially latest-reservation demand, stale reservation-node handling, scheduler ordering, and default profile fallback behavior.
- Keep the change scoped and commit it locally without pushing or changing branches.

## Implementation Steps

1. Add failing regression tests in `tests/unit/service/test_metrics.py` for SQL aggregation of default DinD fallback counts and requested-scope pre-filtering of capacity queue reservation ranking.
2. Promote repository helper entry points to public names while keeping internal/backward-compatible aliases for existing local tests and call sites.
3. Update `metrics.py` imports and use public helper names.
4. Replace Python profile iteration in `_defaulted_dind_slots_for_session` with a SQL `sum(case(...))` aggregate over unresolved workspaces.
5. Join the capacity queue reservation ranking subquery to an aliased requested-workspace scope before applying `row_number()`.
6. Run the narrow metrics tests, then the broader Python validation commands justified by the touched modules.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_repository_coverage.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_metrics.py tests/unit/db/test_repository_coverage.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

All commands must pass before completion. If a broader command cannot run in the workspace, document the blocker in the validation file.
