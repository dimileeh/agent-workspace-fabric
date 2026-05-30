# Address Review Comment 4578892384 Plan

## Problem Statement and Scope

PR review comment `issue:4578892384` raised two review-level concerns:

- `_profile_from_resolved_profile_snapshot` mutates a detached `Workspace`
  instance in memory so downstream executor code sees the persisted profile
  winner. This is safe in the current flow, but future callers should be warned
  not to reattach that mutated object to a SQLAlchemy session.
- Custom-profile planning artifact filtering relies on a strict generated
  workspace ID shape. That strictness is intentional for custom paths, but the
  format contract should not silently drift away from `new_workspace_id()`.

Scope is limited to executor helper documentation, shared ID/owned-path contract
clarity, and targeted regression tests. No GitHub writes, pushes, branch changes,
or broad AWF/CI validation will be performed.

## Requirements Checklist

- Preserve stricter custom-profile artifact filtering so shorthand files like
  `docs/alternate/ws_123.md` remain ordinary owned paths when the workspace ID is
  unknown.
- Keep default `docs/awf-plans/ws_*` classification broad and profile
  independent.
- Make generated workspace ID format and custom-profile artifact matching share
  one explicit contract or test-backed boundary.
- Add a warning at the detached `Workspace` mutation point so future callers do
  not reattach a dirty ORM object after in-memory realignment.
- Run focused tests for the touched helper behavior only.

## Implementation Steps

1. Add targeted failing tests around the generated workspace ID contract and
   custom-profile matching.
2. Update code/comments/docstrings so owned-path matching uses the shared
   workspace ID contract from the ID helper module.
3. Add the detached ORM warning to `_profile_from_resolved_profile_snapshot`.
4. Run the targeted unit tests that cover the changed behavior.
5. Write `plans/ADDRESS_REVIEW_4578892384_VALIDATION.md` with evidence and
   note that AWF/GitHub own broad validation after agent completion.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py tests/unit/control/test_executor_runtime_profile_snapshot.py -q`

Pass criteria: the targeted tests pass and no broad validation suite is run
inside this agent phase.
