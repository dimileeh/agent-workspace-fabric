import assert from "node:assert/strict";
import test from "node:test";

import { notifyEmbeddedParent } from "./console-api-session.ts";

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
