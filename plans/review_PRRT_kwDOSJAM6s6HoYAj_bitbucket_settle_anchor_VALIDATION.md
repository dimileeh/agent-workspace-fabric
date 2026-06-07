# Validation — PRRT_kwDOSJAM6s6HoYAj

## Verdict: FIXED (real merge-gate correctness bug)

The Bitbucket `fetch_pr_status` now mirrors the GitHub path: it derives
`latest_external_review_activity_at` from external (non-viewer, non-deleted) PR
comments and folds it through the shared `_quiet_period_anchor`, setting all four
activity/anchor fields on `PRStatus`. The non-check-reviewer settle gate therefore
re-anchors the quiet window on a late Bitbucket reviewer comment instead of decaying
to the head-only `done_key`, closing the "merge immediately after head settle" gap.

## Changes
- `src/awf/common/bitbucket_client_parsing.py`: new pure
  `latest_external_review_activity(comments, *, account_id)`.
- `src/awf/common/bitbucket_client.py`: `fetch_pr_status` computes latest activity +
  quiet anchor and passes them to `PRStatus`; imports
  `latest_external_review_activity`, `parse_bb_datetime`, and the shared
  `_quiet_period_anchor`.
- Tests added in
  `tests/unit/common/test_bitbucket_client_parts/test_bitbucket_client_part_005.py`.

## Focused checks (green)
- `pytest tests/unit/common/test_bitbucket_client_parts` → 138 passed.
- `ruff check` + `ruff format --check` on changed files → clean.
- `mypy` (pyproject `files = ["src/"]`) → Success, no issues in 357 files.

New behavior branches (newest-external pick, viewer/deleted/no-id/no-timestamp skip,
inline vs general source, anchor-set vs anchor-None) are covered by the added tests;
the existing empty-comment status tests exercise the anchor-None path. Full
suite + coverage gate run under AWF/GitHub after agent completion.
