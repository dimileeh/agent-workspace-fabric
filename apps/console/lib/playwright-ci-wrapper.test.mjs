import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const {
  normalizePlaywrightArgs,
  shouldSkipPlaywrightInstall,
} = require("../scripts/playwright-ci-wrapper.cjs");

test("normal Playwright commands are delegated unchanged", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["test", "--list"], { CI: "true" }),
    ["test", "--list"],
  );
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "chromium"], {}),
    ["install", "chromium"],
  );
});

test("CI Chromium installs are skipped because CI uses the hosted Chrome channel", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "chromium"], { CI: "true" }),
    ["install", "chromium"],
  );
  assert.equal(
    shouldSkipPlaywrightInstall(["install", "chromium"], { CI: "true" }),
    true,
  );
});

test("CI Chromium install skips even when npm forwards installer flags", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "--with-deps", "--force", "chromium"], { CI: "true" }),
    ["install", "--with-deps", "--force", "chromium"],
  );
  assert.equal(
    shouldSkipPlaywrightInstall(["install", "--with-deps", "--force", "chromium"], { CI: "true" }),
    true,
  );
});

test("local Chromium install still delegates to Playwright", () => {
  assert.equal(shouldSkipPlaywrightInstall(["install", "chromium"], {}), false);
});

test("CI install leaves non-Chromium browser requests unchanged", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "firefox"], { CI: "true" }),
    ["install", "firefox"],
  );
});
