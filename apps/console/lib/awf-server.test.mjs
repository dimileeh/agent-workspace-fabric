import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import {
  AWF_STREAM_MAX_TAIL_BYTES,
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

test("sanitizeStreamChannels falls back to the default set when empty or unknown", () => {
  const defaults = "events,agent,validation,services";
  assert.equal(sanitizeStreamChannels(null), defaults);
  assert.equal(sanitizeStreamChannels(""), defaults);
  assert.equal(sanitizeStreamChannels("bogus,nope"), defaults);
});

test("sanitizeTailBytes clamps to the supported range", () => {
  assert.equal(sanitizeTailBytes("1024"), "1024");
  assert.equal(sanitizeTailBytes("999999999"), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes("-5"), "0");
});

test("sanitizeTailBytes falls back to the cap for missing or non-numeric input", () => {
  assert.equal(sanitizeTailBytes(null), String(AWF_STREAM_MAX_TAIL_BYTES));
  assert.equal(sanitizeTailBytes("not-a-number"), String(AWF_STREAM_MAX_TAIL_BYTES));
});

function restoreEnv(name, value) {
  if (value === undefined) {
    delete process.env[name];
    return;
  }
  process.env[name] = value;
}
