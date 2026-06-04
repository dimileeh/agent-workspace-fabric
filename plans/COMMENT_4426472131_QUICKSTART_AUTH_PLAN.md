# Comment 4426472131 Quickstart Auth Plan

## Problem Statement And Scope

CodeRabbit reported that the mocked-first-run command blocks in
`docs/QUICKSTART.md` still show `gh auth token`, which can make mocked-local
users think GitHub CLI authentication is required. The fix is limited to the
Quickstart mocked smoke lane command blocks and their focused docs regression
coverage.

## Requirements Checklist

- Verify the existing Quickstart snippets still contain the reported
  `gh auth token` guidance.
- Update the three lane command blocks so GitHub authentication is clearly
  optional and skippable for mocked smoke.
- Provide a non-CLI-token alternative such as manually supplying
  `AWF_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`.
- Keep the change docs-only plus focused docs tests; do not change runtime
  behavior or broad documentation surfaces.
- Run focused validation only. AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Update the Quickstart docs regression to expect the optional/skippable auth
   wording and no `gh auth token` invocation in `docs/QUICKSTART.md`.
2. Confirm the focused test fails against the current docs.
3. Replace each lane's `gh auth token` comment with optional skip/manual-token
   guidance.
4. Re-run the focused docs test.
5. Record validation evidence in
   `plans/COMMENT_4426472131_QUICKSTART_AUTH_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -k quickstart_mocked_smoke_keeps_github_auth_optional -q`
  - Passes after the docs update.
  - Before the docs update, fails because the current Quickstart still contains
    `gh auth token`.

Broad AWF/GitHub validation is intentionally not run in the agent phase.
