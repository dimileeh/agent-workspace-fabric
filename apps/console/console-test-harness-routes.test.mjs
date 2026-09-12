import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_PAGE_EXTENSIONS,
  HARNESS_PAGE_EXTENSION,
  isConsoleTestHarnessRouteBuildEnabled,
  resolveConsolePageExtensions,
} from "./console-test-harness-routes.ts";

test("harness route build is disabled by default", () => {
  assert.equal(isConsoleTestHarnessRouteBuildEnabled({}), false);
  assert.deepEqual(resolveConsolePageExtensions({}), [...DEFAULT_PAGE_EXTENSIONS]);
});

test("harness route build stays disabled in production even when the flag is set", () => {
  const env = { AWF_CONSOLE_TEST_HARNESS: "1", NODE_ENV: "production" };
  assert.equal(isConsoleTestHarnessRouteBuildEnabled(env), false);
  assert.deepEqual(resolveConsolePageExtensions(env), [...DEFAULT_PAGE_EXTENSIONS]);
  assert.ok(!resolveConsolePageExtensions(env).includes(HARNESS_PAGE_EXTENSION));
});

test("harness route build enables page.harness.tsx only for non-production with the flag", () => {
  const env = { AWF_CONSOLE_TEST_HARNESS: "1", NODE_ENV: "development" };
  assert.equal(isConsoleTestHarnessRouteBuildEnabled(env), true);
  assert.deepEqual(resolveConsolePageExtensions(env), [
    ...DEFAULT_PAGE_EXTENSIONS,
    HARNESS_PAGE_EXTENSION,
  ]);
});

test("harness flag alone without NODE_ENV still enables the test-only extension", () => {
  // next dev may leave NODE_ENV unset; treat that as non-production.
  const env = { AWF_CONSOLE_TEST_HARNESS: "1" };
  assert.equal(isConsoleTestHarnessRouteBuildEnabled(env), true);
  assert.ok(resolveConsolePageExtensions(env).includes(HARNESS_PAGE_EXTENSION));
});
