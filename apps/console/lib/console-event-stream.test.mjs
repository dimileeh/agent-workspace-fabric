import assert from "node:assert/strict";
import test from "node:test";

import { openConsoleEventStream } from "./console-event-stream.ts";

function sse(chunks) {
  return new Response(new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { headers: { "content-type": "text/event-stream; charset=utf-8" } });
}

test("replacing a stream before its first connection only opens the replacement", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => sse(["data: connected\n\n"]));
  const discarded = openConsoleEventStream("/stream?selection=previous");
  discarded.close();
  const replacement = openConsoleEventStream("/stream?selection=current");
  const message = await new Promise(resolve => {
    replacement.onmessage = ({ data }) => {
      replacement.close();
      resolve(data);
    };
  });

  assert.equal(message, "connected");
  assert.deepEqual(fetch.mock.calls.map(call => call.arguments[0]), ["/stream?selection=current"]);
});

test("stream joins chunked multiline messages, honors retry, and closes intentionally", async (t) => {
  const messages = [];
  let connectionCount = 0;
  t.mock.method(globalThis, "fetch", async (_path, init) => {
    connectionCount++;
    assert.equal(init.credentials, "same-origin");
    assert.equal(new Headers(init.headers).get("accept"), "text/event-stream");
    assert.equal(new Headers(init.headers).get("last-event-id"), connectionCount > 1 ? "4" : null);
    return connectionCount > 1
      ? sse(["data: last\r\n\r\n"])
      : sse([": heartbeat\r", "\ndata: fir", "st\r\ndata: line\r\n\r\nretry: 1\nid: 4\nevent: internal\ndata: hidden\n\ndata: incomplete"]);
  });
  const stream = openConsoleEventStream("/stream");
  let disconnects = 0;
  stream.onerror = () => { disconnects++; };
  await new Promise(resolve => {
    stream.onmessage = ({ data }) => {
      messages.push(data);
      if (data === "last") {
        stream.close();
        resolve();
      }
    };
  });
  assert.deepEqual(messages, ["first\nline", "last"]);
  assert.equal(disconnects, 1);
  assert.equal(stream.closed, true);
});

test("a rejected HTTP handshake closes the stream without retrying", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 403 }));
  const stream = openConsoleEventStream("/stream");
  await new Promise(resolve => { stream.onerror = resolve; });
  assert.equal(stream.closed, true);
  assert.equal(fetch.mock.callCount(), 1);
});
