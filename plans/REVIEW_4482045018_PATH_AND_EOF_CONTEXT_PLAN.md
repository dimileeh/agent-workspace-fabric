# Review 4482045018 Path And EOF Context Plan

## Problem Statement And Scope

Address the latest actionable review-level feedback on PR #264 comment
`issue:4482045018`. The scope is limited to `awf init` bootstrap path
resolution and the line-oriented env seed merge behavior.

## Requirements Checklist

- Add a regression test showing asset-root fallback paths are anchored to the
  resolved asset root even when called from a subdirectory and the Compose file
  is missing.
- Add a regression test showing trailing overlay comments for a shared key are
  emitted at the end of the merged env output, not in the middle before later
  seed keys.
- Preserve existing root-env fallback behavior when Compose assets are not
  available.
- Keep overlay comments, duplicate handling, and root-only key behavior intact.
- Commit the fix locally and print the required AWF verdict.

## Implementation Steps

1. Add focused unit tests in `tests/unit/cli/test_init.py` for both review
   issues.
2. Run the new tests and confirm they fail against the current implementation.
3. Update `_resolve_service_compose_paths` to return asset-root-anchored root
   fallback paths when an asset root is provided but the Compose file is absent.
4. Update `_merge_env_seed_contents_with_overlay_keys` so trailing context from
   a matching overlay seed key is appended after the seed pass.
5. Run the focused test file plus lint/type checks for the touched Python code.
6. Record validation in
   `plans/REVIEW_4482045018_PATH_AND_EOF_CONTEXT_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes.
