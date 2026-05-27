# PR288 Console System Chrome Validation

Plan reference: `plans/PR288_CONSOLE_SYSTEM_CHROME_PLAN.md`

## Requirement Status

- Preserve the CI browser smoke step as a real Playwright browser test:
  Complete. `apps/console/playwright.config.ts` uses Playwright's hosted-runner
  Chrome channel only under `CI`, so `npm --prefix apps/console run
  test:browser` still launches a real browser instead of removing the smoke.
- Avoid the timed-out Playwright browser download/install during CI setup:
  Complete. `apps/console/scripts/playwright-ci-wrapper.cjs` now exits
  successfully for CI Chromium install commands and logs that CI uses the hosted
  runner Chrome channel.
- Keep non-CI/local Playwright install behavior unchanged: Complete. The wrapper
  only skips when `CI` is set and the command is `install ... chromium`;
  non-CI installs and non-install commands still delegate to Playwright.
- Add focused regression coverage for the CI wrapper behavior: Complete.
  `apps/console/lib/playwright-ci-wrapper.test.mjs` covers CI skip behavior,
  forwarded installer flags, local delegation, and non-Chromium delegation.
- Run focused console verification only: Complete. Full frontend build/browser
  smoke and broader AWF/GitHub validation are left to AWF/GitHub.
- Commit the fix locally: Complete once this validation file is committed with
  the code changes.

## Evidence

Files changed:

- `apps/console/scripts/playwright-ci-wrapper.cjs`
- `apps/console/playwright.config.ts`
- `apps/console/lib/playwright-ci-wrapper.test.mjs`
- `plans/PR288_CONSOLE_SYSTEM_CHROME_PLAN.md`
- `plans/PR288_CONSOLE_SYSTEM_CHROME_VALIDATION.md`

Focused commands run:

- `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/playwright-ci-wrapper.test.mjs`
  - Result: Passed, `5` tests.
- `npm exec eslint scripts/playwright-ci-wrapper.cjs lib/playwright-ci-wrapper.test.mjs playwright.config.ts`
  - Result: Passed.
- `npm --prefix apps/console run typecheck`
  - Result: Passed.
- `CI=true node scripts/playwright-ci-wrapper.cjs install chromium`
  - Result: Passed quickly and printed the CI system-Chrome skip message.
- `node scripts/prepare-playwright-ci-bin.mjs`
  - Result: Passed; refreshed the local `node_modules/.bin/playwright` wrapper
    copy used by `npm exec`.
- `CI=true npm --prefix apps/console exec playwright install --with-deps chromium`
  - Result: Passed quickly and printed the CI system-Chrome skip message.

Full repository tests, full coverage, full frontend builds, and CI-equivalent
validation were not run locally per the AWF workspace contract; AWF/GitHub own
that broad validation, provenance, logs, timeouts, and merge gating after this
agent phase.
