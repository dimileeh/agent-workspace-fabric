# PRRT_kwDOSJAM6s6F3RF Custom Artifact Wildcards Plan

## Problem Statement And Scope

The review thread reports that custom planning artifact wildcards are filtered
at workspace creation, when no workspace id is available, but later become
ordinary inter-workspace owned paths once callers pass the concrete workspace id.
For a profile such as `docs/alternate/{workspace_id}.md` and persisted
`owned_paths=["docs/alternate/ws_*.md"]`, merge-queue, lock-risk, and overlap
graph callers can then see sibling workspaces as conflicting on an internal AWF
artifact wildcard.

Scope is limited to `src/awf/common/owned_paths.py`, focused unit coverage in
`tests/unit/common/test_owned_paths.py`, and this plan/validation record.

## Requirements Checklist

- Preserve custom profile wildcard artifact scopes after workspace creation when
  a concrete workspace id is available.
- Continue filtering the concrete custom artifact path for the current
  workspace.
- Do not broaden custom wildcard matching to arbitrary `ws_` documentation names
  such as `ws_protocol.md`.
- Preserve workspace-specific parent directory artifact scope behavior.
- Run only focused validation for the changed helper; AWF/GitHub own broad
  validation after the agent phase.

## Implementation Steps

1. Add a failing regression test showing known-workspace custom artifact paths
   still filter a persisted generated `ws_*` wildcard owned path.
2. Confirm the focused regression fails before implementation.
3. Update configured artifact matching so a persisted `ws_*` owned-path scope
   matches the concrete artifact path rendered after workspace creation.
4. Adjust affected unit expectations while preserving real-doc negative cases.
5. Re-run focused owned-path tests and narrow lint for the touched files.

## Assumptions/Changes

- Existing regression coverage treats generated-looking `ws_...` docs as real
  repository paths when the concrete workspace id is known. The implementation
  therefore does not add broad wildcard companions to known-id profile output;
  it filters the persisted literal wildcard scope without filtering sibling
  concrete `ws_...` paths.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py::<regression> -q`
  should fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  should pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py`
  should pass after implementation.

Full AWF/GitHub validation is intentionally not run in this agent phase.
