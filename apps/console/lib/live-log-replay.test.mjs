import assert from "node:assert/strict";
import test from "node:test";

import { streamOffsetAfterLiveFrame, visibleLiveLogFrame } from "./live-log-replay.ts";

function tail(streamId, data, offset, nextOffset) {
  return {
    kind: "tail",
    key: `tail:ws_logs:${streamId}:${offset}:${nextOffset}`,
    workspaceId: "ws_logs",
    streamId,
    data,
  };
}

test("a live frame already inside the tail byte range is not appended", () => {
  const entries = [tail("active.stdout", "recovered-active-inspector-tail", 0, 30)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 20,
    data: "inspector-partial-recover",
  });
  assert.equal(visible, null);
});

test("a frame that starts inside the tail and continues past it keeps only the new suffix", () => {
  const entries = [tail("active.stdout", "café", 0, 5)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 8,
    data: "caféXYZ",
  });
  assert.deepEqual(visible, { offset: 5, nextOffset: 8, data: "XYZ" });
});

test("multibyte tail coverage uses next_offset, not JavaScript string length", () => {
  // "café" is 4 UTF-16 code units and 5 UTF-8 bytes. A replay at byte 4 is
  // still inside the snapshot; string length would let it through.
  const entries = [tail("active.stdout", "café", 0, 5)];
  assert.equal("café".length, 4);
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 4,
    next_offset: 5,
    data: "é",
  });
  assert.equal(visible, null);
  assert.equal(
    streamOffsetAfterLiveFrame(5, { offset: 4, next_offset: 5 }),
    5,
  );
});

test("a frame that begins at the tail boundary is kept, and denial lines are not coverage", () => {
  const entries = [
    {
      kind: "tail",
      key: "tail-error:ws_logs:active.stdout:1",
      workspaceId: "ws_logs",
      streamId: "active.stdout",
      data: "Unable to load log stream",
    },
    tail("quiet.stdout", "other-stream-must-not-cover", 0, 80),
  ];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 3,
    data: "new",
  });
  assert.deepEqual(visible, { offset: 0, nextOffset: 3, data: "new" });
  assert.equal(streamOffsetAfterLiveFrame(5, { offset: 2, next_offset: 9 }), 9);
});
