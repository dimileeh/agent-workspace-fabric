import assert from "node:assert/strict";
import test from "node:test";

import { consoleApiFetch, isConsoleSessionExpired, notifyEmbeddedParent } from "./console-api-session.ts";

test("an inaccessible parent is never sent a notification", () => {
  const location = { origin: "https://console.example", search: "?embedded=1" };
  const postMessage = test.mock.fn();
  const parent = {
    get location() {
      throw new DOMException("Cross-origin parent", "SecurityError");
    },
    postMessage,
  };
  notifyEmbeddedParent({ location, parent });
  assert.equal(postMessage.mock.callCount(), 0);
});

test("opaque child origins and mismatched parent origins are skipped", () => {
  for (const [origin, parentOrigin] of [
    ["null", "null"],
    ["https://console.example", "https://different.example"],
  ]) {
    const postMessage = test.mock.fn();
    notifyEmbeddedParent({
      location: { origin, search: "?embedded=1" },
      parent: { location: { origin: parentOrigin }, postMessage },
    });
    assert.equal(postMessage.mock.callCount(), 0);
  }
});

test("a 401 cancels active requests and rejects subsequent requests at the boundary", async (t) => {
  const location = { origin: "https://console.example", search: "?embedded=1" };
  const postMessage = t.mock.fn();
  globalThis.window = { location, parent: { location, postMessage } };
  t.after(() => { delete globalThis.window; });

  const fetch = t.mock.method(globalThis, "fetch", async (path, init) => {
    if (path === "/held") {
      return new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
      });
    }
    return new Response(null, { status: 401 });
  });
  const controller = new AbortController();
  const held = assert.rejects(consoleApiFetch("/held", { signal: controller.signal }), { name: "AbortError" });
  const denied = assert.rejects(consoleApiFetch("/401", {}), { name: "AbortError" });
  await Promise.all([held, denied]);
  await assert.rejects(consoleApiFetch("/later", {}), { name: "AbortError" });
  assert.equal(isConsoleSessionExpired(), true);
  assert.equal(controller.signal.aborted, false);
  assert.equal(fetch.mock.callCount(), 2);
  assert.deepEqual(postMessage.mock.calls.map(call => call.arguments), [
    [{ type: "awf-console:unauthenticated" }, location.origin],
  ]);
});
