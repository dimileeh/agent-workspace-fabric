# Review 4387467328 Install Manifest Docs Plan

## Problem Statement And Scope

Address review-level comment `4387467328` for PR #303. The review summarized
three actionable findings around the install manifest slice and one nitpick
around the release docs test. Verify each finding against the current branch,
fix only still-valid issues, and avoid protected workflow edits.

## Requirements Checklist

- Confirm `generated_at` values supplied to `scripts/generate_install_manifest.py`
  are validated before manifest output.
- Confirm repository URLs with params, query strings, or fragments are rejected
  before artifact URL assembly.
- Confirm the markdown validation table row for the publish workflow keeps the
  shell pipe from breaking the table.
- Add the missing release docs assertion for `auto` channel semantics if it is
  still absent.
- Run focused validation for the touched docs test and record that broad
  AWF/GitHub validation is managed after agent completion.

## Implementation Steps

1. Inspect the generator, existing tests, release docs, and T11 validation doc.
2. Leave already-satisfied findings unchanged.
3. Add a minimal `auto` assertion beside the existing `stable` and
   `prerelease` assertions in `tests/unit/docs/test_release_docs.py`.
4. Create a validation note with requirement-by-requirement status and focused
   command evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_release_docs.py -q
```

Pass criteria: the focused docs regression test passes. Full repository
validation, coverage gates, frontend builds, pushes, PR creation, and PR
monitoring remain owned by AWF/GitHub after agent completion.
