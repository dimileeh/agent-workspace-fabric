import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const __dirname = dirname(fileURLToPath(import.meta.url));
const lockfilePath = resolve(__dirname, "..", "package-lock.json");
const lockfile = JSON.parse(readFileSync(lockfilePath, "utf8"));

function parseVersion(v) {
  return v.split(".").map(Number);
}

function versionAtLeast(actual, minimum) {
  const a = parseVersion(actual);
  const m = parseVersion(minimum);
  for (let i = 0; i < Math.max(a.length, m.length); i++) {
    const av = a[i] ?? 0;
    const mv = m[i] ?? 0;
    if (av > mv) return true;
    if (av < mv) return false;
  }
  return true;
}

function getPackageVersion(name) {
  const key = `node_modules/${name}`;
  const pkg = lockfile.packages[key];
  assert.ok(pkg, `Expected package ${name} to exist in package-lock.json`);
  return pkg.version;
}

// @babel/core security bump: 7.29.0 -> 7.29.7
test("@babel/core is at least 7.29.7 (security patch)", () => {
  const version = getPackageVersion("@babel/core");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/core version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/code-frame is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/code-frame");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/code-frame version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/compat-data is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/compat-data");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/compat-data version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/generator is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/generator");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/generator version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-compilation-targets is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-compilation-targets");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-compilation-targets version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-globals is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-globals");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-globals version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-module-imports is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-module-imports");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-module-imports version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-module-transforms is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-module-transforms");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-module-transforms version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-string-parser is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-string-parser");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-string-parser version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-validator-identifier is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-validator-identifier");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-validator-identifier version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helper-validator-option is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helper-validator-option");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helper-validator-option version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/helpers is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/helpers");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/helpers version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/parser is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/parser");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/parser version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/template is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/template");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/template version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/traverse is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/traverse");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/traverse version ${version} is below minimum 7.29.7`,
  );
});

test("@babel/types is at least 7.29.7", () => {
  const version = getPackageVersion("@babel/types");
  assert.ok(
    versionAtLeast(version, "7.29.7"),
    `@babel/types version ${version} is below minimum 7.29.7`,
  );
});

test("electron-to-chromium is at least 1.5.376", () => {
  const version = getPackageVersion("electron-to-chromium");
  assert.ok(
    versionAtLeast(version, "1.5.376"),
    `electron-to-chromium version ${version} is below minimum 1.5.376`,
  );
});

test("node-releases is at least 2.0.48", () => {
  const version = getPackageVersion("node-releases");
  assert.ok(
    versionAtLeast(version, "2.0.48"),
    `node-releases version ${version} is below minimum 2.0.48`,
  );
});

// Regression: fsevents should remain marked as a dev dependency after the lockfile fix
test("fsevents is marked as dev dependency", () => {
  const key = "node_modules/fsevents";
  const pkg = lockfile.packages[key];
  assert.ok(pkg, "Expected fsevents to exist in package-lock.json");
  assert.equal(
    pkg.dev,
    true,
    "fsevents should be marked as dev:true (regression guard for lockfile fix)",
  );
});

// Structural sanity checks on the lock file
test("package-lock.json has lockfileVersion 3", () => {
  assert.equal(lockfile.lockfileVersion, 3);
});

test("versionAtLeast returns true for equal versions", () => {
  assert.ok(versionAtLeast("7.29.7", "7.29.7"));
});

test("versionAtLeast returns false when actual is below minimum", () => {
  assert.ok(!versionAtLeast("7.29.0", "7.29.7"));
  assert.ok(!versionAtLeast("7.28.6", "7.29.7"));
  assert.ok(!versionAtLeast("6.99.9", "7.29.7"));
});

test("versionAtLeast returns true when actual is above minimum", () => {
  assert.ok(versionAtLeast("7.30.0", "7.29.7"));
  assert.ok(versionAtLeast("8.0.0", "7.29.7"));
  assert.ok(versionAtLeast("7.29.8", "7.29.7"));
});