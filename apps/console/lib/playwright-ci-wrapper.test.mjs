import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { shouldSkipPlaywrightInstall } = require("../scripts/playwright-ci-wrapper.cjs");

test("normal Playwright commands are delegated", () => {
  assert.equal(shouldSkipPlaywrightInstall(["test", "--list"], { CI: "true" }), false);
  assert.equal(shouldSkipPlaywrightInstall(["install", "chromium"], {}), false);
});

test("CI Chromium installs are skipped because CI uses the hosted Chrome channel", () => {
  assert.equal(
    shouldSkipPlaywrightInstall(["install", "chromium"], { CI: "true" }),
    true,
  );
});

test("CI Chromium install skips even when npm forwards installer flags", () => {
  assert.equal(
    shouldSkipPlaywrightInstall(["install", "--with-deps", "--force", "chromium"], { CI: "true" }),
    true,
  );
});

test("CI install leaves non-Chromium browser requests delegated", () => {
  assert.equal(shouldSkipPlaywrightInstall(["install", "firefox"], { CI: "true" }), false);
  assert.equal(
    shouldSkipPlaywrightInstall(["install", "chromium", "firefox"], { CI: "true" }),
    false,
  );
});
