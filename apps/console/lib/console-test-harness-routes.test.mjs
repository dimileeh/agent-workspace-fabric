/**
 * Lives under lib/ so `npm test` (`lib/*.test.mjs` in package.json) executes it.
 * Do not move this file back to the console package root — that path is outside
 * the unit-test glob and would leave route-graph guarantees unenforced.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_PAGE_EXTENSIONS,
  HARNESS_PAGE_EXTENSION,
  isConsoleTestHarnessRouteBuildEnabled,
  resolveConsoleDevIndicators,
  resolveConsolePageExtensions,
} from "../console-test-harness-routes.ts";

test("harness route build is disabled by default", () => {
  assert.equal(isConsoleTestHarnessRouteBuildEnabled({}), false);
  assert.deepEqual(resolveConsolePageExtensions({}), [...DEFAULT_PAGE_EXTENSIONS]);
});

test("harness route build ignores non-exact flag values", () => {
  for (const value of ["0", "true", "yes", ""]) {
    const env = { AWF_CONSOLE_TEST_HARNESS: value, NODE_ENV: "development" };
    assert.equal(isConsoleTestHarnessRouteBuildEnabled(env), false);
    assert.deepEqual(resolveConsolePageExtensions(env), [...DEFAULT_PAGE_EXTENSIONS]);
  }
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

test("harness dev servers hide the Next.js dev indicator", () => {
  // The indicator fetches its own font and re-renders on client URL changes,
  // which lands inside the browser smoke pane timing windows.
  for (const env of [
    { AWF_CONSOLE_TEST_HARNESS: "1", NODE_ENV: "development" },
    { AWF_CONSOLE_TEST_HARNESS: "1" },
  ]) {
    assert.equal(resolveConsoleDevIndicators(env), false);
  }
});

test("developer and production servers keep the default Next.js dev indicator", () => {
  for (const env of [
    {},
    { AWF_CONSOLE_TEST_HARNESS: "0", NODE_ENV: "development" },
    { AWF_CONSOLE_TEST_HARNESS: "1", NODE_ENV: "production" },
  ]) {
    assert.equal(resolveConsoleDevIndicators(env), undefined);
  }
});
