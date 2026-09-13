import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

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
