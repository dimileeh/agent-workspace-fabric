import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { handleWorkspaceControlRoute } from "./workspace-control-routes.ts";

const originalFetch = globalThis.fetch;
const originalBaseUrl = process.env.AWF_API_BASE_URL;
const originalToken = process.env.AWF_API_TOKEN;
const originalCryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");

afterEach(() => {
  globalThis.fetch = originalFetch;
  process.env.AWF_API_BASE_URL = originalBaseUrl;
  process.env.AWF_API_TOKEN = originalToken;
  if (originalCryptoDescriptor) {
    Object.defineProperty(globalThis, "crypto", originalCryptoDescriptor);
  }
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

  const request = jsonRequest(
    {
      reason: "resume PR monitor",
      workspace_version: 7,
      idempotency_key: "browser-idem",
      ignored: "must not forward",
    },
    { cookie: "console-session=browser" },
  );
  assert.equal(request.headers.get("cookie"), "console-session=browser");

  const response = await handleWorkspaceControlRoute("remonitor", "ws_123", request);

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

test("cancel BFF maps to cancel endpoint and sends stop_stack", async () => {
  const calls = [];
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_TOKEN = "server-token";
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse({
      workspace_id: "ws_123",
      operation_id: "op_cancel",
      operation_status: "succeeded",
      status: "cancelled",
      message: "cancelled",
    });
  };

  const response = await handleWorkspaceControlRoute(
    "cancel",
    "ws_123",
    jsonRequest({
      reason: "operator console cancel",
      workspace_version: 7,
      idempotency_key: "cancel-idem",
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://awf.example.test/v1/workspaces/ws_123/cancel");
  assert.equal(calls[0].init.method, "POST");
  const headers = normalizeHeaders(calls[0].init.headers);
  assert.equal(headers.authorization, "Bearer server-token");
  assert.equal(headers["idempotency-key"], "cancel-idem");
  assert.equal(headers["if-match"], "7");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    reason: "operator console cancel",
    stop_stack: true,
  });
});

test("cancel BFF sends stop_stack even without a reason", async () => {
  const calls = recordAwfProxy({
    workspace_id: "ws_123",
    operation_id: "op_cancel",
    operation_status: "succeeded",
    status: "cancelled",
    message: "cancelled",
  });

  const response = await handleWorkspaceControlRoute("cancel", "ws_123", rawRequest(""));

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].init.body), { stop_stack: true });
});

test("cancel BFF rejects requested_tier", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  const response = await handleWorkspaceControlRoute(
    "cancel",
    "ws_123",
    jsonRequest({ requested_tier: 1 }),
  );

  assert.equal(called, false);
  assert.equal(response.status, 400);
  await assertInvalidRequest(response, "requested_tier is only supported for revalidate.");
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

test("BFF generates idempotency key without global crypto", async () => {
  const calls = [];
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_TOKEN = "server-token";
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: undefined,
  });
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse({
      workspace_id: "ws_123",
      operation_id: "op_refresh",
      operation_status: "pending",
      status: "queued",
      message: "refresh queued",
    });
  };

  const response = await handleWorkspaceControlRoute("refresh", "ws_123", jsonRequest({}));

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  const headers = normalizeHeaders(calls[0].init.headers);
  assert.match(headers["idempotency-key"], /^console:[0-9a-f-]{36}$/);
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

test("BFF rejects malformed and non-object JSON bodies before proxying", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  for (const { body, message } of [
    { body: "{", message: "Request body must be valid JSON." },
    { body: "null", message: "Request body must be a JSON object." },
    { body: "[]", message: "Request body must be a JSON object." },
  ]) {
    const response = await handleWorkspaceControlRoute("refresh", "ws_123", rawRequest(body));

    assert.equal(response.status, 400);
    await assertInvalidRequest(response, message);
  }
  assert.equal(called, false);
});

test("BFF accepts an empty request body as an empty control payload", async () => {
  const calls = recordAwfProxy({
    workspace_id: "ws_123",
    operation_id: "op_refresh",
    operation_status: "pending",
    status: "queued",
    message: "refresh queued",
  });

  const response = await handleWorkspaceControlRoute("refresh", "ws_123", rawRequest(""));

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://awf.example.test/v1/workspaces/ws_123/refresh");
  assert.deepEqual(JSON.parse(calls[0].init.body), {});
});

test("BFF validates requested tier values and action support", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  for (const invalidTier of [0, 1.5, 4]) {
    const response = await handleWorkspaceControlRoute(
      "revalidate",
      "ws_123",
      jsonRequest({ requested_tier: invalidTier }),
    );

    assert.equal(response.status, 400);
    await assertInvalidRequest(response, "requested_tier must be 1, 2, or 3.");
  }

  const unsupportedActionResponse = await handleWorkspaceControlRoute(
    "refresh",
    "ws_123",
    jsonRequest({ requested_tier: 1 }),
  );
  assert.equal(unsupportedActionResponse.status, 400);
  await assertInvalidRequest(
    unsupportedActionResponse,
    "requested_tier is only supported for revalidate.",
  );
  assert.equal(called, false);
});

test("BFF proxies valid requested tier values for revalidate", async () => {
  const calls = recordAwfProxy(
    {
      id: "op_validate",
      workspace_id: "ws_123",
      type: "validate",
      status: "pending",
    },
    { status: 202 },
  );

  for (const tier of [1, 2, 3]) {
    const response = await handleWorkspaceControlRoute(
      "revalidate",
      "ws_123",
      jsonRequest({ requested_tier: tier, idempotency_key: `tier-${tier}` }),
    );

    assert.equal(response.status, 202);
  }

  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map((call) => JSON.parse(call.init.body)), [
    { requested_tier: 1 },
    { requested_tier: 2 },
    { requested_tier: 3 },
  ]);
});

test("BFF validates workspace version before proxying", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  for (const workspaceVersion of [1.5, -1]) {
    const response = await handleWorkspaceControlRoute(
      "refresh",
      "ws_123",
      jsonRequest({ workspace_version: workspaceVersion }),
    );

    assert.equal(response.status, 400);
    await assertInvalidRequest(response, "workspace_version must be a non-negative integer.");
  }

  assert.equal(called, false);
});

test("BFF gives body idempotency and workspace version precedence over headers", async () => {
  const calls = recordAwfProxy({
    workspace_id: "ws_123",
    operation_id: "op_refresh",
    operation_status: "pending",
    status: "queued",
    message: "refresh queued",
  });

  const response = await handleWorkspaceControlRoute(
    "refresh",
    "ws_123",
    jsonRequest(
      { workspace_version: 11, idempotency_key: "body-idem" },
      { "idempotency-key": "header-idem", "if-match": "10" },
    ),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  const headers = normalizeHeaders(calls[0].init.headers);
  assert.equal(headers["idempotency-key"], "body-idem");
  assert.equal(headers["if-match"], "11");
});

test("BFF forwards idempotency and if-match headers when body values are absent", async () => {
  const calls = recordAwfProxy({
    workspace_id: "ws_123",
    operation_id: "op_remonitor",
    operation_status: "pending",
    status: "queued",
    message: "remonitor queued",
  });

  const response = await handleWorkspaceControlRoute(
    "remonitor",
    "ws_123",
    jsonRequest({}, { "idempotency-key": "header-idem", "if-match": "12" }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  const headers = normalizeHeaders(calls[0].init.headers);
  assert.equal(headers["idempotency-key"], "header-idem");
  assert.equal(headers["if-match"], "12");
});

test("BFF rejects idempotency keys longer than AWF control storage limit", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({});
  };

  const response = await handleWorkspaceControlRoute(
    "refresh",
    "ws_123",
    jsonRequest({ idempotency_key: "k".repeat(129) }),
  );

  assert.equal(called, false);
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    ok: false,
    error_code: "INVALID_REQUEST",
    message: "idempotency_key must be 128 characters or fewer.",
  });
});

async function assertInvalidRequest(response, message) {
  assert.deepEqual(await response.json(), {
    ok: false,
    error_code: "INVALID_REQUEST",
    message,
  });
}

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

function rawRequest(body, headers = {}) {
  return new Request("https://console.example.test/operator", {
    method: "POST",
    headers: {
      authorization: "Bearer browser-token",
      "content-type": "application/json",
      ...headers,
    },
    body,
  });
}

function recordAwfProxy(responseBody, responseInit = {}) {
  const calls = [];
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_TOKEN = "server-token";
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return jsonResponse(responseBody, responseInit);
  };
  return calls;
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
