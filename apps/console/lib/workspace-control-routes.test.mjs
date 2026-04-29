import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { handleWorkspaceControlRoute } from "./workspace-control-routes.ts";

const originalFetch = globalThis.fetch;
const originalBaseUrl = process.env.AWF_API_BASE_URL;
const originalToken = process.env.AWF_API_TOKEN;

afterEach(() => {
  globalThis.fetch = originalFetch;
  process.env.AWF_API_BASE_URL = originalBaseUrl;
  process.env.AWF_API_TOKEN = originalToken;
});

test("remonitor BFF posts with server token and idempotency key", async () => {
  const calls = [];
  process.env.AWF_API_BASE_URL = "https://awf.example.test/";
  process.env.AWF_API_TOKEN = "server-token";
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse({
      workspace_id: "ws_123",
      operation_id: "op_remonitor",
      operation_status: "succeeded",
      status: "monitoring_pr",
      message: "monitor resumed",
    });
  };

  const response = await handleWorkspaceControlRoute(
    "remonitor",
    "ws_123",
    jsonRequest({
      reason: "resume PR monitor",
      workspace_version: 7,
      idempotency_key: "browser-idem",
      ignored: "must not forward",
    }),
    { cookie: "console-session=browser" },
  );

  const responseText = await response.text();
  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://awf.example.test/v1/workspaces/ws_123/remonitor");
  assert.equal(calls[0].init.method, "POST");
  const headers = normalizeHeaders(calls[0].init.headers);
  assert.equal(headers.authorization, "Bearer server-token");
  assert.equal(headers["idempotency-key"], "browser-idem");
  assert.equal(headers["if-match"], "7");
  assert.equal(headers["content-type"], "application/json");
  assert.equal(headers.cookie, undefined);
  assert.deepEqual(JSON.parse(calls[0].init.body), { reason: "resume PR monitor" });
  assert.equal(responseText.includes("server-token"), false);
});

test("refresh BFF preserves structured AWF errors", async () => {
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_TOKEN = "server-token";
  globalThis.fetch = async () =>
    jsonResponse(
      {
        detail: {
          error_code: "WORKSPACE_STATE_NOT_REFRESHABLE",
          message: "Workspace cannot be refreshed from this state.",
        },
      },
      { status: 409 },
    );

  const response = await handleWorkspaceControlRoute(
    "refresh",
    "ws_123",
    jsonRequest({ reason: "stale target", workspace_version: 3 }),
  );

  assert.equal(response.status, 409);
  assert.deepEqual(await response.json(), {
    detail: {
      error_code: "WORKSPACE_STATE_NOT_REFRESHABLE",
      message: "Workspace cannot be refreshed from this state.",
    },
  });
});

test("revalidate BFF maps to validate endpoint and requested tier", async () => {
  const calls = [];
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_TOKEN = "server-token";
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse(
      {
        id: "op_validate",
        workspace_id: "ws_123",
        type: "validate",
        status: "pending",
        error_code: null,
        error_message: null,
        payload: { requested_tier: 2 },
        result: null,
        idempotency_key: "validate-idem",
        created_at: "2026-04-29T12:00:00Z",
        started_at: null,
        finished_at: null,
        owner: "operator_api",
        source: "operator_api",
        action: null,
        pr_number: null,
        pr_url: null,
        source_head_sha: null,
        source_base_sha: null,
        reason: "rerun required validation",
        reason_code: "OPERATOR_VALIDATE",
        failure_code: null,
        failure_message: null,
        log_stream_refs: {},
        log_stream_ids: [],
      },
      { status: 202 },
    );
  };

  const response = await handleWorkspaceControlRoute(
    "revalidate",
    "ws_123",
    jsonRequest({
      reason: "rerun required validation",
      requested_tier: 2,
      workspace_version: 7,
      idempotency_key: "validate-idem",
    }),
  );

  assert.equal(response.status, 202);
  assert.equal(calls[0].url, "https://awf.example.test/v1/workspaces/ws_123/validate");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    reason: "rerun required validation",
    requested_tier: 2,
  });
  assert.deepEqual(await response.json(), {
    id: "op_validate",
    workspace_id: "ws_123",
    type: "validate",
    status: "pending",
    error_code: null,
    error_message: null,
    payload: { requested_tier: 2 },
    result: null,
    idempotency_key: "validate-idem",
    created_at: "2026-04-29T12:00:00Z",
    started_at: null,
    finished_at: null,
    owner: "operator_api",
    source: "operator_api",
    action: null,
    pr_number: null,
    pr_url: null,
    source_head_sha: null,
    source_base_sha: null,
    reason: "rerun required validation",
    reason_code: "OPERATOR_VALIDATE",
    failure_code: null,
    failure_message: null,
    log_stream_refs: {},
    log_stream_ids: [],
  });
});

test("BFF rejects invalid revalidate tier before proxying", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  const response = await handleWorkspaceControlRoute(
    "revalidate",
    "ws_123",
    jsonRequest({ requested_tier: 4, idempotency_key: "validate-idem" }),
  );

  assert.equal(called, false);
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    ok: false,
    error_code: "INVALID_REQUEST",
    message: "requested_tier must be 1, 2, or 3.",
  });
});

function jsonRequest(body, headers = {}) {
  return new Request("https://console.example.test/operator", {
    method: "POST",
    headers: {
      authorization: "Bearer browser-token",
      "content-type": "application/json",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json" },
  });
}

function normalizeHeaders(value) {
  return Object.fromEntries(new Headers(value).entries());
}
