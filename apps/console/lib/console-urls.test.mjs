import assert from "node:assert/strict";
import test from "node:test";

import {
  awfPath,
  consoleHref,
  getConsoleUrlConfig,
  normalizeBasePath,
  normalizeServiceBase,
  operatorPath,
} from "./console-urls.ts";

test("default config matches local console path defaults", () => {
  assert.deepEqual(getConsoleUrlConfig({}), {
    basePath: "",
    apiBase: "/api/awf",
    operatorBase: "/api/operator",
  });
});

test("hosted config reads public path bases", () => {
  assert.deepEqual(
    getConsoleUrlConfig({
      NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH: "/workspaces",
      NEXT_PUBLIC_AWF_CONSOLE_API_BASE: "/workspaces/api/awf",
      NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE: "/workspaces/api/operator",
    }),
    {
      basePath: "/workspaces",
      apiBase: "/workspaces/api/awf",
      operatorBase: "/workspaces/api/operator",
    },
  );
});

test("normalizeBasePath strips trailing slashes and empty values", () => {
  assert.equal(normalizeBasePath(undefined), "");
  assert.equal(normalizeBasePath(""), "");
  assert.equal(normalizeBasePath("/"), "");
  assert.equal(normalizeBasePath("workspaces"), "/workspaces");
  assert.equal(normalizeBasePath("/workspaces/"), "/workspaces");
});

test("normalizeServiceBase applies defaults and slash normalization", () => {
  assert.equal(normalizeServiceBase(undefined, "/api/awf"), "/api/awf");
  assert.equal(normalizeServiceBase("api/awf/", "/api/awf"), "/api/awf");
  assert.equal(
    normalizeServiceBase("/workspaces/api/awf/", "/api/awf"),
    "/workspaces/api/awf",
  );
});

test("local awfPath and operatorPath match today's hardcoded strings", () => {
  const previous = {
    base: process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH,
    api: process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE,
    operator: process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE,
  };
  delete process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH;
  delete process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE;
  delete process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE;
  try {
    assert.equal(awfPath("health"), "/api/awf/health");
    assert.equal(
      awfPath("workspaces/overview", { limit: 100, status: "running" }),
      "/api/awf/workspaces/overview?limit=100&status=running",
    );
    assert.equal(
      awfPath(`workspaces/${encodeURIComponent("ws 1")}/stream`, {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
      "/api/awf/workspaces/ws%201/stream?channels=events%2Cagent%2Cvalidation%2Cservices&tail_bytes=65536",
    );
    assert.equal(
      awfPath(`workspaces/${encodeURIComponent("ws 1")}/artifacts/download`, {
        path: "conformance.json",
      }),
      "/api/awf/workspaces/ws%201/artifacts/download?path=conformance.json",
    );
    assert.equal(
      operatorPath(`workspaces/${encodeURIComponent("ws 1")}/cancel`),
      "/api/operator/workspaces/ws%201/cancel",
    );
    assert.equal(
      operatorPath(`workspaces/ws_1/remonitor`),
      "/api/operator/workspaces/ws_1/remonitor",
    );
    assert.equal(consoleHref("/"), "/");
  } finally {
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH", previous.base);
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_API_BASE", previous.api);
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE", previous.operator);
  }
});

test("hosted awfPath and operatorPath use configured same-origin bases", () => {
  const previous = {
    base: process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH,
    api: process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE,
    operator: process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE,
  };
  process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH = "/workspaces";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE = "/workspaces/api/awf";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE = "/workspaces/api/operator";
  try {
    assert.equal(awfPath("health"), "/workspaces/api/awf/health");
    assert.equal(
      awfPath("workspaces/overview", { limit: 100 }),
      "/workspaces/api/awf/workspaces/overview?limit=100",
    );
    assert.equal(
      awfPath("workspaces/ws_1"),
      "/workspaces/api/awf/workspaces/ws_1",
    );
    assert.equal(
      awfPath("workspaces/ws_1/stream", {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
      "/workspaces/api/awf/workspaces/ws_1/stream?channels=events%2Cagent%2Cvalidation%2Cservices&tail_bytes=65536",
    );
    assert.equal(
      awfPath("workspaces/ws_1/artifacts/download", { path: "plan.md" }),
      "/workspaces/api/awf/workspaces/ws_1/artifacts/download?path=plan.md",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/cancel"),
      "/workspaces/api/operator/workspaces/ws_1/cancel",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/refresh"),
      "/workspaces/api/operator/workspaces/ws_1/refresh",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/revalidate"),
      "/workspaces/api/operator/workspaces/ws_1/revalidate",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/remonitor"),
      "/workspaces/api/operator/workspaces/ws_1/remonitor",
    );
    assert.equal(consoleHref("/"), "/workspaces");
    assert.equal(consoleHref(""), "/workspaces");
  } finally {
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH", previous.base);
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_API_BASE", previous.api);
    restoreEnv("NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE", previous.operator);
  }
});

function restoreEnv(name, previous) {
  if (previous === undefined) {
    delete process.env[name];
    return;
  }
  process.env[name] = previous;
}
