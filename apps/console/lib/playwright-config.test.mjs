import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { ESLint } from "eslint";

test("harness build output is ignored by ESLint while source remains lintable", async () => {
  const eslint = new ESLint({ cwd: new URL("../", import.meta.url).pathname });
  assert.equal(await eslint.isPathIgnored(".next-harness/server/app/page.js"), true);
  assert.equal(await eslint.isPathIgnored("playwright.config.ts"), false);
});

test("harness servers isolate build artifacts from ordinary development", () => {
  const config = JSON.parse(execFileSync(process.execPath, [
    "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON",
    "--input-type=module",
    "-e",
    'import config from "./playwright.config.ts"; console.log(JSON.stringify(config));',
  ], {
    cwd: new URL("../", import.meta.url),
    env: { ...process.env, AWF_CONSOLE_DIST_DIR: ".next" },
    encoding: "utf8",
  }));
  const distDirs = config.webServer.map((server) => server.env.AWF_CONSOLE_DIST_DIR);
  assert.deepEqual(distDirs, [".next-harness", ".next-hosted"]);
});

for (const ci of ["", "1"]) {
  test(`browser workers stay bounded at two with CI=${JSON.stringify(ci)}`, () => {
    const config = JSON.parse(execFileSync(process.execPath, [
      "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON",
      "--input-type=module",
      "-e",
      'import config from "./playwright.config.ts"; console.log(JSON.stringify(config));',
    ], {
      cwd: new URL("../", import.meta.url),
      env: { ...process.env, CI: ci },
      encoding: "utf8",
    }));
    assert.equal(config.workers, 2);
    // Keep within-file ordering and explicit serial suites intact.
    assert.notEqual(config.fullyParallel, true);
  });
}
