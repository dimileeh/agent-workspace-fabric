# Review Comment 4578892384 Plan

## Problem Statement and Scope

Address the PR #313 review-level feedback about runtime profile snapshot
coupling and staleness snapshot noise without changing broad merge-safety
behavior.

Scope is limited to:

- executor runtime profile snapshot resolution/sync helpers and tests;
- staleness candidate snapshot owned-path fallback behavior and tests;
- plan/validation artifacts required by `plans/PLAN_EXECUTION_PROTOCOL.md`.

## Requirements Checklist

- `_profile_for_workspace` must not stamp a locally resolved profile onto the
  ORM workspace object before `_sync_resolved_profile` can enforce
  first-write-wins semantics.
- `_sync_resolved_profile` must remain responsible for persisting the winning
  snapshot and realigning the active workspace object.
- Staleness snapshots must prefer meaningful attempt-owned paths when workspace
  paths are only internal plan artifacts.
- Existing advisory plan-artifact staleness behavior must remain intact when no
  meaningful attempt-owned fallback exists.
- Add focused regression coverage before implementation.
- Use only targeted validation commands; full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Implementation Steps

1. Update executor runtime profile snapshot tests to encode the pure resolver
   contract and sync-owned mutation.
2. Update staleness tests to assert that artifact-only workspace ownership with
   real attempt ownership snapshots only the real attempt paths.
3. Run the targeted tests and confirm the new assertions fail before code
   changes where practical.
4. Remove the `ws.resolved_profile` mutation from `_profile_for_workspace` and
   clarify the helper comment/docstring.
5. Change `_snapshot_owned_paths` to return filtered real attempt paths when
   workspace-owned paths are only plan artifacts and such attempt paths exist.
6. Run the focused test files and focused `ruff check` on touched source/tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py tests/unit/service/test_staleness_parts/test_staleness_part_002.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/helpers.py src/awf/service/staleness.py tests/unit/control/test_executor_runtime_profile_snapshot.py tests/unit/service/test_staleness_parts/test_staleness_part_002.py`
  must pass.
- Full repository validation, coverage, and GitHub checks are intentionally not
  run in this agent phase per the AWF workspace contract.
