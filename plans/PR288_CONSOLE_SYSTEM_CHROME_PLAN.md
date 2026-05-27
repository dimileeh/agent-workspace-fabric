# PR288 Console System Chrome Plan

## Problem Statement and Scope

The current PR #288 GitHub CI run no longer reproduces the stale Python
readiness assertion, but the `console` job now times out at the 15-minute job
limit while running `npm --prefix apps/console exec playwright install
--with-deps chromium`. The log shows npm 11 treating `--with-deps` as npm
configuration and the app-installed Playwright wrapper downloading the Chromium
headless shell before hanging until cancellation.

Scope is limited to the console Playwright CI bootstrap owned by the app:
`apps/console/scripts/playwright-ci-wrapper.cjs`, the Playwright config, focused
console tests, and plan/validation docs. Do not edit protected GitHub workflow
files, weaken the browser smoke check, switch branches, push, or run the full
frontend build suite locally.

## Requirements Checklist

- Preserve the CI browser smoke step as a real Playwright browser test.
- Avoid the timed-out Playwright browser download/install during CI setup.
- Keep non-CI/local Playwright install behavior unchanged.
- Add focused regression coverage for the CI wrapper behavior.
- Run focused console verification only; leave broad AWF/GitHub validation to
  AWF/GitHub after the agent phase.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Patch the Playwright CI wrapper so `playwright install chromium` in CI exits
   successfully after documenting that CI uses the hosted runner Chrome channel.
2. Configure `apps/console/playwright.config.ts` to use the `chrome` channel
   only when `CI` is set, so browser smoke still launches a real browser.
3. Update `apps/console/lib/playwright-ci-wrapper.test.mjs` to cover no-op CI
   install behavior and local/non-install delegation.
4. Run focused console tests for the wrapper and typecheck the touched config.
5. Record validation evidence in
   `plans/PR288_CONSOLE_SYSTEM_CHROME_VALIDATION.md`.
6. Commit the focused fix locally.

## Verification Commands and Pass Criteria

- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/playwright-ci-wrapper.test.mjs`
  - Passes wrapper regression tests.
- `npm --prefix apps/console run typecheck`
  - Passes TypeScript validation for the changed Playwright config.
- `CI=true npm --prefix apps/console exec playwright install --with-deps chromium`
  - Exits quickly through the refreshed CI wrapper without downloading a browser.

Full repository tests, full coverage, full frontend builds, and CI-equivalent
validation are intentionally not run locally; AWF/GitHub own broad validation
after the agent phase.
