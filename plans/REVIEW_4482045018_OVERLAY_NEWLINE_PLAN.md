# Review 4482045018 Overlay Newline Plan

## Problem Statement And Scope

Address PR review comment `issue:4482045018` about dotenv merge corruption in
`awf init`. When the root `.env` overlay's last shared assignment has no
trailing newline, replacing a template assignment can concatenate the following
seed assignment into the same physical line.

Scope is limited to the line-oriented env merge helper and a focused regression
test.

## Requirements Checklist

- [x] Add a failing regression test for a shared overlay assignment at EOF with
  no trailing newline.
- [x] Normalize stored overlay assignment lines so replacements preserve line
  boundaries.
- [x] Keep existing merge ordering, comments, and overlay-only key behavior
  unchanged.
- [x] Run focused unit tests and narrow lint for the changed files.
- [x] Create a validation artifact mapping evidence back to this plan.
- [x] Commit the scoped fix on the current AWF branch without pushing.

## Implementation Steps

1. Add a focused regression in `tests/unit/cli/test_init.py` near the existing
   env merge tests.
2. Run that regression before implementation and confirm it fails.
3. Update `_merge_env_seed_contents_with_overlay_keys()` in
   `src/awf/cli/main.py` to store overlay assignment lines with a trailing line
   ending when missing.
4. Re-run the focused regression, then the merge-related init tests and ruff on
   touched files.
5. Record validation evidence in
   `plans/REVIEW_4482045018_OVERLAY_NEWLINE_VALIDATION.md`.
6. Stage only changed files and commit with a conventional review-fix message.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_merge_env_seed_normalizes_overlay_assignment_without_trailing_newline -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py
```

Pass criteria: the focused regression fails before implementation and passes
afterwards; the broader init test file and narrow ruff check pass after the fix.
