# PR288 Console Playwright Timeout Plan

## Problem Statement And Scope

PR #288's required CI gate fails because the aggregate `ci-required` job sees
the `console` job as cancelled. GitHub Actions logs show console lint,
typecheck, and build passed, then the job hit its 15-minute timeout while
running `npm --prefix apps/console exec playwright install --with-deps chromium`.
The workflow command is in a protected CI file, so this fix must avoid editing
`.github/workflows/ci.yml`.

Scope is limited to the console package install/test tooling and focused plan
artifacts.

## Requirements Checklist

- [ ] Do not switch branches, push, rebase, or edit protected workflow files.
- [ ] Preserve the browser smoke check; do not skip or weaken CI validation.
- [ ] Reduce the CI Playwright install surface to the headless Chromium browser
      artifact used by the existing headless smoke tests.
- [ ] Add focused regression coverage for the argument normalization.
- [ ] Run focused console verification only; full AWF/GitHub validation remains
      owned by AWF after agent completion.

## Implementation Steps

1. Add a focused Node unit test for the CI Playwright installer argument
   normalization.
2. Add a small Playwright CLI wrapper that delegates all commands unchanged,
   except CI `playwright install chromium`, which becomes
   `playwright install --only-shell chromium`.
3. Add a console package `postinstall` hook that installs the wrapper into
   `node_modules/.bin/playwright` after `npm ci`.
4. Verify with the focused Node test and a dry-run Playwright install command
   that shows the full Chrome bundle is no longer requested.

## Verification Commands

- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/playwright-ci-wrapper.test.mjs`
- `npm --prefix apps/console ci`
- `CI=true npm --prefix apps/console exec -- playwright install --dry-run chromium`
- `CI=true npm --prefix apps/console exec playwright install --with-deps chromium`
- `npm --prefix apps/console run test:browser -- --list`
- `npm --prefix apps/console run test:browser -- dashboard-filters.spec.ts -g "dashboard filters"`
- `npm exec -- eslint scripts/playwright-ci-wrapper.cjs scripts/prepare-playwright-ci-bin.mjs lib/playwright-ci-wrapper.test.mjs` from `apps/console`

Pass criteria: the unit test passes, `npm ci` completes and installs the local
wrapper, the dry-run install lists Chromium headless shell instead of full
Chrome for Testing, and Playwright test discovery still delegates to the real
test runner.
