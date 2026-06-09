# Review 4664994375 Jinja2 Tojson Plan

## Problem Statement and Scope

Address the review-level comment on PR #485 for `scripts/ci/check_j2_tojson.py`.
The reviewer identified two quality gaps in the new Jinja2 escaping guard:

- Duplicate allowlist directives for the same normalized raw expression are silently absorbed.
- Per-line lint diagnostics are printed as plain stderr lines instead of GitHub Actions error annotations.

Scope is limited to the checker script and focused unit coverage for those behaviors.

## Requirements Checklist

- Verify the reviewer claims against the local implementation before editing.
- Add a regression test that duplicate allowlist directives for the same expression produce a diagnostic even when the raw interpolation exists.
- Add or update a focused test proving per-line diagnostics are emitted in GitHub Actions annotation format.
- Preserve useful `path:line` diagnostic content for local CLI readability.
- Keep existing allowlist, stale-entry, and escaping behavior intact.
- Run only targeted tests for the changed checker behavior; AWF/GitHub own broad validation after agent completion.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Add focused failing tests in `tests/unit/scripts/test_check_j2_tojson.py`.
2. Update `scripts/ci/check_j2_tojson.py` to report duplicate allowlist directives.
3. Update diagnostic output to use GitHub Actions `::error file=...,line=...,title=...::...` syntax for per-line diagnostics while retaining the existing `path:line` text in the message.
4. Run the targeted unit test file only.
5. Create `plans/REVIEW_4664994375_J2_TOJSON_VALIDATION.md` with requirement-by-requirement status and evidence.
6. Stage only changed files and commit with a conventional review-fix message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`

Pass criteria: the focused checker tests pass, including regressions for duplicate allowlist directives and GitHub Actions annotation formatting.
Full AWF/GitHub validation is intentionally not run inside the agent phase.
