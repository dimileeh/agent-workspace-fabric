import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { normalizePlaywrightArgs } = require("../scripts/playwright-ci-wrapper.cjs");

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

test("CI Chromium installs use the headless shell artifact", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "chromium"], { CI: "true" }),
    ["install", "--only-shell", "chromium"],
  );
});

test("CI Chromium install keeps installer flags that remain relevant", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "--dry-run", "--force", "chromium"], { CI: "true" }),
    ["install", "--dry-run", "--force", "--only-shell", "chromium"],
  );
});

test("CI Chromium install preserves unknown installer flags", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "--trace", "--debug", "chromium"], { CI: "true" }),
    ["install", "--trace", "--debug", "--only-shell", "chromium"],
  );
});

test("CI install leaves non-Chromium browser requests unchanged", () => {
  assert.deepEqual(
    normalizePlaywrightArgs(["install", "firefox"], { CI: "true" }),
    ["install", "firefox"],
  );
});
