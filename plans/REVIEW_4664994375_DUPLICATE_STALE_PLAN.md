# Review 4664994375 Duplicate Stale Diagnostic Plan

## Problem Statement and Scope

Address the remaining review-level issue for PR #485 in
`scripts/ci/check_j2_tojson.py`: duplicate allowlist directives are already
reported, but stale-allowlist detection still iterates over duplicate directives.
When the duplicated expression is now escaped with `| tojson`, the duplicate
line receives both a duplicate diagnostic and a stale diagnostic.

Scope is limited to the checker's stale allowlist diagnostic behavior and focused
unit coverage for that edge case.

## Requirements Checklist

- Verify the duplicate-plus-stale claim against the local implementation before
  editing.
- Add a regression test where duplicate allowlist directives target an expression
  that is now escaped.
- Ensure duplicate directives are not also reported as stale on the same line.
- Preserve stale diagnostics for the canonical allowlist directive when the
  expression is no longer used raw.
- Keep existing duplicate, stale, allowlist, and escaping behavior intact.
- Run only targeted checker tests; AWF/GitHub own broad validation after agent
  completion.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Add a focused failing regression test in
   `tests/unit/scripts/test_check_j2_tojson.py`.
2. Update `scripts/ci/check_j2_tojson.py` so stale detection scans only the
   canonical non-duplicate allowlist directives.
3. Run the targeted checker test file.
4. Create `plans/REVIEW_4664994375_DUPLICATE_STALE_VALIDATION.md` with
   requirement-by-requirement status and evidence.
5. Stage only changed files and commit with a conventional review-fix message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`

Pass criteria: the focused checker tests pass, including a regression proving a
duplicate directive line is not also reported as stale. Full AWF/GitHub
validation is intentionally not run inside the agent phase.
