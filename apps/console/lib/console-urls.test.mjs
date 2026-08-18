import assert from "node:assert/strict";
import test from "node:test";

import {
  awfPath,
  consoleHref,
  getConsoleUrlConfig,
  normalizeBasePath,
  normalizeServiceBase,
  operatorPath,
  parseContextQueryKeys,
} from "./console-urls.ts";

test("default config matches local console path defaults", () => {
  assert.deepEqual(getConsoleUrlConfig({}), {
    basePath: "",
    apiBase: "/api/awf",
    operatorBase: "/api/operator",
    contextQueryKeys: [],
  });
});

test("hosted config reads public path bases and context query keys", () => {
  assert.deepEqual(
    getConsoleUrlConfig({
      NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH: "/workspaces",
      NEXT_PUBLIC_AWF_CONSOLE_API_BASE: "/api/core-console",
      NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE: "/api/core-console",
      NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS: "org_id,project_id",
    }),
    {
      basePath: "/workspaces",
      apiBase: "/api/core-console",
      operatorBase: "/api/core-console",
      contextQueryKeys: ["org_id", "project_id"],
    },
  );
});

test("empty or whitespace-only context query keys env yields no carry", () => {
  assert.deepEqual(
    getConsoleUrlConfig({
      NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS: "",
    }).contextQueryKeys,
    [],
  );
  assert.deepEqual(
    getConsoleUrlConfig({
      NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS: "  ,  , ",
    }).contextQueryKeys,
    [],
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
    normalizeServiceBase("/api/core-console/", "/api/awf"),
    "/api/core-console",
  );
});

test("parseContextQueryKeys is deterministic, bounded, and fail-closed", () => {
  assert.deepEqual(parseContextQueryKeys(undefined), []);
  assert.deepEqual(parseContextQueryKeys("org_id, project_id"), ["org_id", "project_id"]);
  assert.deepEqual(parseContextQueryKeys("org_id,org_id,project_id"), ["org_id", "project_id"]);
  assert.deepEqual(parseContextQueryKeys("1bad,org_id,has-dash"), ["org_id"]);
  assert.deepEqual(
    parseContextQueryKeys("a,b,c,d,e,f,g,h,i,j"),
    ["a", "b", "c", "d", "e", "f", "g", "h"],
  );
  const tooLong = `${"a".repeat(65)}`;
  assert.deepEqual(parseContextQueryKeys(`${tooLong},org_id`), ["org_id"]);
});

test("forbidden configured context key names are omitted", () => {
  assert.deepEqual(
    parseContextQueryKeys(
      "token,secret,password,api_key,authorization,cookie,credential,org_id,My_Token,API_KEY",
    ),
    ["org_id"],
  );
  assert.deepEqual(
    parseContextQueryKeys("user_token,session_secret,auth_cookie,x_credential_y,project_id"),
    ["project_id"],
  );
});

test("local awfPath and operatorPath match today's hardcoded strings", () => {
  const previous = snapshotEnv();
  clearConsoleEnv();
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
    // Page coords present but empty context keys → no carry (byte-compatible).
    assert.equal(
      awfPath("health", undefined, "?org_id=o1&project_id=p1"),
      "/api/awf/health",
    );
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("hosted awfPath and operatorPath use /api/core-console bases", () => {
  const previous = snapshotEnv();
  process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH = "/workspaces";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS = "org_id,project_id";
  try {
    assert.equal(awfPath("health"), "/api/core-console/health");
    assert.equal(
      awfPath("workspaces/overview", { limit: 100 }),
      "/api/core-console/workspaces/overview?limit=100",
    );
    assert.equal(awfPath("workspaces/ws_1"), "/api/core-console/workspaces/ws_1");
    assert.equal(
      awfPath("workspaces/ws_1/stream", {
        channels: "events,agent,validation,services",
        tail_bytes: 65536,
      }),
      "/api/core-console/workspaces/ws_1/stream?channels=events%2Cagent%2Cvalidation%2Cservices&tail_bytes=65536",
    );
    assert.equal(
      awfPath("workspaces/ws_1/artifacts/download", { path: "plan.md" }),
      "/api/core-console/workspaces/ws_1/artifacts/download?path=plan.md",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/cancel"),
      "/api/core-console/workspaces/ws_1/cancel",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/refresh"),
      "/api/core-console/workspaces/ws_1/refresh",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/revalidate"),
      "/api/core-console/workspaces/ws_1/revalidate",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/remonitor"),
      "/api/core-console/workspaces/ws_1/remonitor",
    );
    assert.equal(consoleHref("/"), "/workspaces");
    assert.equal(consoleHref(""), "/workspaces");
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("hosted API and operator URLs carry both page coordinates", () => {
  const previous = snapshotEnv();
  applyHostedEnv();
  try {
    const page = "?org_id=org_a&project_id=proj_b&noise=1";
    assert.equal(
      awfPath("health", undefined, page),
      "/api/core-console/health?org_id=org_a&project_id=proj_b",
    );
    assert.equal(
      awfPath("workspaces/overview", { limit: 50 }, page),
      "/api/core-console/workspaces/overview?limit=50&org_id=org_a&project_id=proj_b",
    );
    assert.equal(
      operatorPath("workspaces/ws_1/cancel", undefined, page),
      "/api/core-console/workspaces/ws_1/cancel?org_id=org_a&project_id=proj_b",
    );
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("missing page coordinates omit context keys", () => {
  const previous = snapshotEnv();
  applyHostedEnv();
  try {
    assert.equal(awfPath("health", undefined, ""), "/api/core-console/health");
    assert.equal(
      awfPath("health", undefined, "?project_id=proj_only"),
      "/api/core-console/health?project_id=proj_only",
    );
    assert.equal(
      awfPath("health", undefined, "?org_id=&project_id=proj_b"),
      "/api/core-console/health?project_id=proj_b",
    );
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("explicit request query takes precedence over page context", () => {
  const previous = snapshotEnv();
  applyHostedEnv();
  try {
    const page = "?org_id=from_page&project_id=proj_page";
    assert.equal(
      awfPath("health", { org_id: "from_caller" }, page),
      "/api/core-console/health?org_id=from_caller&project_id=proj_page",
    );
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("consoleHref does not inject configured context keys", () => {
  const previous = snapshotEnv();
  applyHostedEnv();
  try {
    // Navigation helpers never read page search / context keys.
    assert.equal(consoleHref("/"), "/workspaces");
    assert.equal(consoleHref("/ws_1", { tab: "logs" }), "/workspaces/ws_1?tab=logs");
  } finally {
    restoreEnvSnapshot(previous);
  }
});

test("forbidden keys never appear on API URLs even if on the page", () => {
  const previous = snapshotEnv();
  process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS =
    "token,api_key,authorization,password,secret,cookie,credential,org_id";
  try {
    assert.equal(
      awfPath("health", undefined, "?token=t&api_key=k&org_id=o1&password=p"),
      "/api/core-console/health?org_id=o1",
    );
  } finally {
    restoreEnvSnapshot(previous);
  }
});

const ENV_KEYS = [
  "NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH",
  "NEXT_PUBLIC_AWF_CONSOLE_API_BASE",
  "NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE",
  "NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS",
];

function snapshotEnv() {
  return Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
}

function clearConsoleEnv() {
  for (const key of ENV_KEYS) {
    delete process.env[key];
  }
}

function applyHostedEnv() {
  process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH = "/workspaces";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE = "/api/core-console";
  process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS = "org_id,project_id";
}

function restoreEnvSnapshot(previous) {
  for (const key of ENV_KEYS) {
    if (previous[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = previous[key];
    }
  }
}
