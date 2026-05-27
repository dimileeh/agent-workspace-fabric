#!/usr/bin/env node
"use strict";

/* eslint-disable @typescript-eslint/no-require-imports */

const { spawnSync } = require("node:child_process");

function isCiChromiumInstall(args, env = process.env) {
  if (!env.CI || args[0] !== "install") {
    return false;
  }

  const installArgs = args.slice(1);
  const browserArgs = installArgs.filter((arg) => !arg.startsWith("-"));
  return browserArgs.length === 1 && browserArgs[0] === "chromium";
}

function shouldSkipPlaywrightInstall(args, env = process.env) {
  return isCiChromiumInstall(args, env);
}

function run() {
  const originalArgs = process.argv.slice(2);
  if (shouldSkipPlaywrightInstall(originalArgs, process.env)) {
    console.error(
      "AWF console CI: using the hosted runner Chrome channel; skipping Playwright browser download.",
    );
    process.exit(0);
  }

  const result = spawnSync(
    process.execPath,
    [require.resolve("@playwright/test/cli"), ...originalArgs],
    { stdio: "inherit" },
  );
  process.exit(result.status ?? 1);
}

if (require.main === module) {
  run();
}

module.exports = { shouldSkipPlaywrightInstall };
