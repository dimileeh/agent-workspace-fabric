import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import {
  AWF_STREAM_MAX_TAIL_BYTES,
  attachWorkspaceStreamHandlers,
  proxyAwf,
  sanitizeStreamChannels,
  sanitizeTailBytes,
} from "./awf-server.ts";

const originalBaseUrl = process.env.AWF_API_BASE_URL;
const originalFetchTimeoutMs = process.env.AWF_API_FETCH_TIMEOUT_MS;
const originalFetch = globalThis.fetch;

afterEach(() => {
  restoreEnv("AWF_API_BASE_URL", originalBaseUrl);
  restoreEnv("AWF_API_FETCH_TIMEOUT_MS", originalFetchTimeoutMs);
  globalThis.fetch = originalFetch;
});

test("proxyAwf aborts hung backend fetches with a bounded timeout", async () => {
  process.env.AWF_API_BASE_URL = "https://awf.example.test";
  process.env.AWF_API_FETCH_TIMEOUT_MS = "10";
  let signal;

  globalThis.fetch = async (_url, init) => {
    signal = init?.signal;
    if (!signal) {
      return new Promise(() => {});
    }
    return new Promise((_resolve, reject) => {
      signal.addEventListener(
        "abort",
        () => {
          reject(new Error("backend fetch aborted by timeout"));
        },
        { once: true },
      );
    });
  };

  const result = await Promise.race([proxyAwf("/v1/workspaces"), delay(250, "still-pending")]);

  assert.notEqual(result, "still-pending");
  assert.ok(signal instanceof AbortSignal);
  assert.equal(signal.aborted, true);

  assert.equal(result.status, 502);
  assert.deepEqual(await result.json(), {
    ok: false,
    error_code: "AWF_API_UNREACHABLE",
    message: "Unable to reach the AWF API.",
    detail: "backend fetch aborted by timeout",
  });
});

test("sanitizeStreamChannels keeps only supported channels", () => {
  assert.equal(sanitizeStreamChannels("agent, events"), "agent,events");
  assert.equal(sanitizeStreamChannels("agent,bogus,services"), "agent,services");
});

test("sanitizeStreamChannels falls back to the default set when omitted or unknown", () => {
  const defaults = "events,agent,validation,services";
  assert.equal(sanitizeStreamChannels(null), defaults);
  assert.equal(sanitizeStreamChannels("bogus,nope"), defaults);
});

test("sanitizeStreamChannels preserves an explicit empty channel list", () => {
  // Snapshot-only subscriptions omit events and log channels. Expanding that
  // to the default set would fetch feeds the client declared unsupported.
  assert.equal(sanitizeStreamChannels(""), "");
  assert.equal(sanitizeStreamChannels("   "), "");
});

test("sanitizeTailBytes clamps to the supported range", () => {
  assert.equal(sanitizeTailBytes("1024"), "1024");
  assert.equal(sanitizeTailBytes("999999999"), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes("-5"), "0");
});

test("sanitizeTailBytes falls back to the cap for missing or non-numeric input", () => {
  assert.equal(sanitizeTailBytes(null), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes("not-a-number"), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes(""), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes("   "), String(AWF_STREAM_MAX_TAIL_BYTES));
});

function makeFakeSocket() {
  const handlers = new Map();
  return {
    closed: 0,
    on(event, handler) {
      handlers.set(event, handler);
      return this;
    },
    emit(event, ...args) {
      const handler = handlers.get(event);
      if (handler) {
        handler(...args);
      }
    },
    close() {
      this.closed += 1;
    },
  };
}

test("attachWorkspaceStreamHandlers surfaces connected, message, and raw frames", () => {
  const socket = makeFakeSocket();
  const sent = [];
  let streamClosed = 0;
  attachWorkspaceStreamHandlers({
    socket,
    workspaceId: "ws-1",
    send: (payload) => sent.push(payload),
    closeStream: () => {
      streamClosed += 1;
    },
  });

  socket.emit("open");
  socket.emit("message", Buffer.from(JSON.stringify({ type: "tick" }), "utf-8"));
  socket.emit("message", Buffer.from("not-json", "utf-8"));

  assert.deepEqual(sent, [
    { type: "connected", workspace_id: "ws-1" },
    { type: "tick" },
    { type: "raw", data: "not-json" },
  ]);
  assert.equal(streamClosed, 0);
});

test("attachWorkspaceStreamHandlers preserves handshake 401/403 on the SSE error frame", () => {
  for (const status of [401, 403]) {
    const socket = makeFakeSocket();
    const sent = [];
    let streamClosed = 0;
    attachWorkspaceStreamHandlers({
      socket,
      workspaceId: "ws-auth",
      send: (payload) => sent.push(payload),
      closeStream: () => {
        streamClosed += 1;
      },
    });

    socket.emit("error", new Error(`Unexpected server response: ${status}`));
    socket.emit("close", 1006, Buffer.from("", "utf-8"));

    assert.deepEqual(sent, [
      {
        type: "error",
        ok: false,
        error_code: status === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
        status,
        message: "Workspace stream authorization denied.",
        detail: `Unexpected server response: ${status}`,
      },
    ]);
    assert.equal(socket.closed, 1);
    assert.equal(streamClosed, 1);
  }
});

test("attachWorkspaceStreamHandlers reads handshake status from the error object", () => {
  for (const [field, status] of [
    ["statusCode", 401],
    ["status", 403],
  ]) {
    const socket = makeFakeSocket();
    const sent = [];
    attachWorkspaceStreamHandlers({
      socket,
      workspaceId: "ws-auth-status",
      send: (payload) => sent.push(payload),
      closeStream: () => {},
    });

    socket.emit("error", { [field]: status, message: "handshake rejected" });

    assert.equal(sent[0]?.status, status);
    assert.equal(sent[0]?.error_code, status === 401 ? "UNAUTHORIZED" : "FORBIDDEN");
    assert.equal(sent[0]?.message, "Workspace stream authorization denied.");
  }
});

test("attachWorkspaceStreamHandlers treats an authorization close as feed denial", () => {
  for (const [reason, status, errorCode] of [
    ["UNAUTHORIZED", 401, "UNAUTHORIZED"],
    ["FORBIDDEN", 403, "FORBIDDEN"],
  ]) {
    const socket = makeFakeSocket();
    const sent = [];
    attachWorkspaceStreamHandlers({
      socket,
      workspaceId: "ws-auth-close",
      send: (payload) => sent.push(payload),
      closeStream: () => {},
    });

    socket.emit("close", 1008, Buffer.from(reason, "utf-8"));

    assert.deepEqual(sent, [
      {
        type: "error",
        ok: false,
        error_code: errorCode,
        status,
        message: "Workspace stream authorization denied.",
        detail: reason,
      },
    ]);
  }
});

test("attachWorkspaceStreamHandlers terminates the stream on a mid-stream error", () => {
  const socket = makeFakeSocket();
  const sent = [];
  let streamClosed = 0;
  attachWorkspaceStreamHandlers({
    socket,
    workspaceId: "ws-2",
    send: (payload) => sent.push(payload),
    closeStream: () => {
      streamClosed += 1;
    },
  });

  // Socket already opened, then fails mid-stream: the error must be terminal,
  // closing the upstream socket and ending the SSE response (regression for
  // "Stream errors leave SSE open").
  socket.emit("open");
  socket.emit("error", new Error("boom"));

  assert.deepEqual(sent, [
    { type: "connected", workspace_id: "ws-2" },
    {
      type: "error",
      ok: false,
      error_code: "AWF_STREAM_ERROR",
      message: "AWF workspace stream failed.",
      detail: "boom",
    },
  ]);
  assert.equal(socket.closed, 1);
  assert.equal(streamClosed, 1);
});

test("attachWorkspaceStreamHandlers ignores a close that follows a terminal error", () => {
  const socket = makeFakeSocket();
  const sent = [];
  let streamClosed = 0;
  attachWorkspaceStreamHandlers({
    socket,
    workspaceId: "ws-2b",
    send: (payload) => sent.push(payload),
    closeStream: () => {
      streamClosed += 1;
    },
  });

  // Our own socket.close() in the error path provokes a follow-up "close":
  // the internal terminal guard must drop it so we emit exactly one terminal
  // frame and call closeStream once.
  socket.emit("error", new Error("boom"));
  socket.emit("close", 1006, Buffer.from("gone", "utf-8"));

  assert.deepEqual(sent, [
    {
      type: "error",
      ok: false,
      error_code: "AWF_STREAM_ERROR",
      message: "AWF workspace stream failed.",
      detail: "boom",
    },
  ]);
  assert.equal(socket.closed, 1);
  assert.equal(streamClosed, 1);
});

test("attachWorkspaceStreamHandlers ends the stream on upstream close", () => {
  const socket = makeFakeSocket();
  const sent = [];
  let streamClosed = 0;
  attachWorkspaceStreamHandlers({
    socket,
    workspaceId: "ws-3",
    send: (payload) => sent.push(payload),
    closeStream: () => {
      streamClosed += 1;
    },
  });

  socket.emit("close", 1006, Buffer.from("gone", "utf-8"));

  assert.deepEqual(sent, [
    { type: "closed", workspace_id: "ws-3", code: 1006, reason: "gone" },
  ]);
  assert.equal(streamClosed, 1);
});

function restoreEnv(name, value) {
  if (value === undefined) {
    delete process.env[name];
    return;
  }
  process.env[name] = value;
}
