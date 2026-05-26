# PRRT_kwDOSJAM6s6E5ENC Import Cycle Plan

## Problem Statement and Scope

The PR review thread reports that `src/awf/common/github_client_adoption.py`
still imports DTO/error types from `awf.common.github_client` at module scope
while `github_client.py` imports adoption helpers at module scope. This leaves a
runtime import cycle when the adoption helper module is imported before
`github_client.py`.

Scope is limited to resolving the import cycle for the review thread without
changing GitHub client behavior.

## Requirements Checklist

- Remove runtime module-scope imports from `github_client_adoption.py` back to
  `github_client.py`.
- Preserve public behavior and structured `PullRequestMetadataError` details.
- Keep type annotations valid without reintroducing runtime imports.
- Validate with the focused split-import regression and focused adoption/GitHub
  client tests.
- Do not run AWF/GitHub-owned broad validation; AWF will run broad validation
  after the agent phase.

## Implementation Steps

1. Confirm the existing focused split-import regression fails against the
   current checkout.
2. Replace module-scope runtime imports in `github_client_adoption.py` with
   `TYPE_CHECKING` imports and local runtime imports at call sites.
3. Re-run the focused split-import regression and the narrow adoption/GitHub
   client test targets touched by the change.
4. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6E5ENC_IMPORT_CYCLE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_split_imports.py -q`
  - Passes, proving split helper modules import in a clean module state.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_adoption_edges.py tests/unit/common/test_github_client_parts/test_github_client_part_001.py::TestFetchPullRequestAdoptionMetadata tests/unit/common/test_github_client_parts/test_github_client_part_001.py::TestBranchOpenPullRequestResolver -q`
  - Passes, proving the local imports preserve adoption metadata and branch-open
    PR parsing behavior.
