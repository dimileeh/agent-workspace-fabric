# PRRT_kwDOSJAM6s6Cr0AH Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Cr0AH_PLAN.md`

## Requirement Status

- Complete: Added a regression proving create-idempotency payload matching
  treats legacy `NULL` stored `owned_paths` as an empty list.
- Complete: Preserved submitted-list semantics for non-empty `owned_paths`.
  Existing parameterized coverage for reordering, deduping, and removals still
  passes.
- Complete: Did not add lifecycle status or phase to create payload equality.
  The matcher now documents that create idempotency replays the existing
  workspace in its current lifecycle state; the new regression sets the
  workspace status to `running` while still matching the original create
  payload.
- Complete: Kept production changes limited to the idempotency matcher.

## Evidence

- Changed `src/awf/service/workspaces.py`.
- Changed `tests/unit/service/test_workspace_idempotency.py`.
- Added `plans/PRRT_kwDOSJAM6s6Cr0AH_PLAN.md`.
- Added this validation file.

## Verification

- Confirmed the new regression failed before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_payload_match_treats_legacy_null_owned_paths_as_empty -q`
  failed with `TypeError: 'NoneType' object is not iterable`.
- Confirmed the regression passed after the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_payload_match_treats_legacy_null_owned_paths_as_empty -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  passed: 30 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py`
  passed.

## Iteration 1

The initial full `test_workspace_idempotency.py` run exposed two legacy v1
idempotency tests that depended on live provider readiness and failed in this
Docker-less AWF workspace. I added a deterministic provider-readiness fixture
only to those v1 idempotency tests so the file validates idempotency behavior
without depending on local Docker/provider state.
