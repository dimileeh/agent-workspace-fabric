# Review Comment 4454403868 Summary Follow-Up Plan

## Problem Statement and Scope

PR #249 received a review-level summary comment with two actionable follow-up
items: document why callback delivery re-checks URL structure that registration
already validates, and avoid repeatedly traversing the cached OpenAPI schema
when marking Authorization headers required. Scope is limited to those two
items and focused regression coverage for the OpenAPI caching behavior.

## Requirements Checklist

- Keep delivery-time callback target validation rules intact.
- Add a concise defense-in-depth comment explaining the duplicate URL-structure
  checks in delivery-time validation.
- Add a regression test proving the OpenAPI auth contract patch is applied once
  and cached for later `app.openapi()` calls.
- Update the OpenAPI wrapper to return the already-patched cached schema on
  subsequent calls.
- Do not push, switch branches, or write any GitHub comment.

## Implementation Steps

1. Add a failing OpenAPI unit test that monkeypatches the auth-contract marker
   and asserts two `app.openapi()` calls only invoke it once while returning the
   same cached schema.
2. Update `src/awf/api/app.py` to short-circuit when `app.openapi_schema` is
   already populated.
3. Add a short defense-in-depth note above the duplicate URL-structure checks in
   `src/awf/service/callbacks.py`.
4. Run the focused OpenAPI test, then relevant API/service unit checks and lint.
5. Record validation evidence in
   `plans/review_comment_4454403868_summary_followup_VALIDATION.md`.
6. Stage only changed files and commit locally with a review-comment fix
   message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k target_policy`
  passes if selected tests exist; otherwise run the callback target validation
  subset that covers delivery-time policy enforcement.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/app.py src/awf/service/callbacks.py tests/unit/api/test_openapi_artifact.py`
  passes.
