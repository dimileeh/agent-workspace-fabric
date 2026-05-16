# Review Comment 4303731394 Workspace Control Route Tests Plan

## Problem Statement And Scope

CodeRabbit's review-level summary for PR #259 reports remaining actionable
coverage gaps around `handleWorkspaceControlRoute` validation and forwarding
logic. The inline-summary items for `.env.example`, `awf-server.ts`, and
`docs/GETTING_STARTED.md` are already reflected in the current branch, so this
plan is scoped to the still-valid nitpick: direct regression tests for the
workspace control BFF route.

The implementation should not change runtime behavior unless the tests expose
an actual mismatch. Current behavior intentionally accepts an empty body as an
empty payload, while malformed or non-object JSON is rejected.

## Assumptions/Changes

- Initial focused validation exposed that `apps/console/lib/awf-server.ts`
  cannot import `NextResponse` from `next/server` under the repository's Node
  ESM test runner and installed Next package. `next/server.js` resolves
  successfully and is required for the existing console tests, so the review
  summary's import recommendation is treated as a false-positive sub-item and
  corrected as part of making the route tests runnable.

## Requirements Checklist

- Add direct tests for malformed JSON and non-object JSON request bodies using
  the `INVALID_REQUEST` response shape.
- Exercise the empty-body parsing branch and prove valid empty payloads still
  proxy to AWF.
- Cover invalid `requested_tier` values and the unsupported action branch for
  `requested_tier`.
- Cover valid `requested_tier` values 1, 2, and 3 for `revalidate`.
- Cover non-integer, negative, and valid `workspace_version` behavior.
- Verify body `idempotency_key` takes precedence over the `Idempotency-Key`
  request header.
- Verify body `workspace_version` takes precedence over the `If-Match` request
  header, and that the header is used when the body value is absent.
- Keep changes minimal and limited to tests plus required plan/validation docs
  unless a real source bug is found.

## Implementation Steps

1. Add helper assertions to `apps/console/lib/workspace-control-routes.test.mjs`
   for invalid request responses and proxied calls.
2. Add focused `node:test` cases for the validation and precedence branches
   listed above.
3. Run the narrow console route test file.
4. Run the console test script if the focused test passes.
5. Record validation evidence in the matching validation document.

## Verification Commands

```bash
npm --prefix apps/console exec -- node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/workspace-control-routes.test.mjs
npm --prefix apps/console run test
```

Pass criteria: the focused route test and console unit test script both pass.
