# Review 4482045018 Review-Level Follow-Up Plan

## Problem Statement and Scope

Address the review-level follow-up on PR comment `issue:4482045018` for local
service env resolution and env overlay merging. Keep the existing conservative
policy that a Docker Compose env file is only forwarded when it belongs to a
verified AWF source checkout.

## Requirements Checklist

- Preserve the current no-verified-root behavior: current-directory `.env` can
  be a settings read source, but it must not be treated as Docker Compose's
  `--env-file`.
- Avoid the redundant second bootstrap asset-root lookup when service commands
  have already resolved the verified local-service Compose env path.
- Preserve context before the first non-final duplicate overlay-only key, even
  when that context is a blank-separated single comment rather than a section
  header or non-comment note.
- Add regression tests before implementation and keep changes scoped to the
  affected CLI/env merge helpers.
- Commit the finished changes locally without pushing or changing branches.

## Implementation Steps

1. Add failing unit tests for:
   - one source-checkout service command resolving the Compose env file without
     calling `get_bootstrap_asset_root()` twice;
   - the overlay merge preserving blank-separated single-comment context before
     a duplicate overlay-only key.
2. Thread a trusted Compose env path from the already-resolved service paths into
   service env-file resolution so the helper can avoid re-validating through a
   second asset-root lookup.
3. Adjust duplicate overlay-only key handling to carry forward any context from
   discarded non-final occurrences.
4. Run narrow unit tests, then lint/type/unit surfaces justified by the touched
   Python CLI helpers.
5. Record plan validation in
   `plans/REVIEW_4482045018_REVIEW_LEVEL_FOLLOWUP_VALIDATION.md`.

## Assumptions/Changes

- The duplicate overlay-only key context observation conflicts with existing
  regression policy in
  `test_init_without_path_deduplicates_root_only_overlay_keys`, which expects
  comments attached to discarded stale root-only duplicates to be omitted. The
  code change for that item is therefore not pursued; the handled outcome for
  that sub-observation is false positive.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
