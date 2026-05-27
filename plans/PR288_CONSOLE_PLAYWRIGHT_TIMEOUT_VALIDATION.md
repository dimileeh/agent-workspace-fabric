# PR288 Console Playwright Timeout Validation

Plan reference: `plans/PR288_CONSOLE_PLAYWRIGHT_TIMEOUT_PLAN.md`

## Requirement Status

- Do not switch branches, push, rebase, or edit protected workflow files:
  Complete.
- Preserve the browser smoke check; do not skip or weaken CI validation:
  Complete. Browser smoke still runs through `playwright test`; the CI install
  now fetches Chromium headless shell, which is the artifact used for headless
  Playwright runs.
- Reduce the CI Playwright install surface:
  Complete. CI `playwright install chromium` is normalized to
  `playwright install --only-shell chromium`.
- Add focused regression coverage:
  Complete. `apps/console/lib/playwright-ci-wrapper.test.mjs` covers command
  delegation and CI install normalization.
- Run focused console verification only:
  Complete. Full AWF/GitHub validation remains owned by AWF after agent
  completion.

## Files Changed

- `apps/console/package.json`
- `apps/console/scripts/playwright-ci-wrapper.cjs`
- `apps/console/scripts/prepare-playwright-ci-bin.mjs`
- `apps/console/lib/playwright-ci-wrapper.test.mjs`
- `plans/PR288_CONSOLE_PLAYWRIGHT_TIMEOUT_PLAN.md`
- `plans/PR288_CONSOLE_PLAYWRIGHT_TIMEOUT_VALIDATION.md`

## Evidence

- Confirmed the failing GitHub Actions `console` job timed out during
  `Install Playwright browser` after lint, typecheck, and build had passed.
- Confirmed the initial focused regression failed before implementation:
  `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/playwright-ci-wrapper.test.mjs`
  failed with missing `../scripts/playwright-ci-wrapper.cjs`.
- Passed focused regression:
  `node --test --disable-warning=MODULE_TYPELESS_PACKAGE_JSON lib/playwright-ci-wrapper.test.mjs`.
- Passed console dependency install and postinstall wrapper setup:
  `npm --prefix apps/console ci`.
- Passed dry-run artifact check:
  `CI=true npm --prefix apps/console exec -- playwright install --dry-run chromium`
  listed `Chrome Headless Shell` and `FFmpeg`, not full Chrome for Testing.
- Passed exact workflow command shape after wrapper install:
  `CI=true npm --prefix apps/console exec playwright install --with-deps chromium`.
- Passed Playwright browser-smoke discovery:
  `npm --prefix apps/console run test:browser -- --list`.
- Passed one launched browser smoke:
  `npm --prefix apps/console run test:browser -- dashboard-filters.spec.ts -g "dashboard filters"`.
- Passed focused lint:
  `npm exec -- eslint scripts/playwright-ci-wrapper.cjs scripts/prepare-playwright-ci-bin.mjs lib/playwright-ci-wrapper.test.mjs`
  from `apps/console`.

## Remaining Risk

The GitHub workflow itself was not edited because `.github/workflows/ci.yml` is
protected and not required for this fix. AWF/GitHub should run the full required
CI suite after this agent completes.
