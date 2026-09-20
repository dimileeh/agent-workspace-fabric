import assert from "node:assert/strict";
import test from "node:test";

import {
  entriesAfterTailSnapshot,
  streamOffsetAfterLiveFrame,
  visibleLiveLogFrame,
} from "./live-log-replay.ts";

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

test("a live payload without next_offset advances the cursor by its UTF-8 byte length", () => {
  // Live LogFrame payloads omit next_offset. "café" is 4 code units and 5
  // UTF-8 bytes; JavaScript string length would leave the reconnect cursor short.
  assert.equal("café".length, 4);
  assert.equal(streamOffsetAfterLiveFrame(0, { offset: 10, data: "café" }), 15);
  assert.equal(streamOffsetAfterLiveFrame(12, { offset: 10, data: "café" }), 15);
  assert.equal(streamOffsetAfterLiveFrame(15, { offset: 10, data: "café" }), 15);
  assert.equal(streamOffsetAfterLiveFrame(20, { offset: 10, data: "café" }), 20);
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

test("a lossy overlap is not sliced by re-encoding replacement text", () => {
  // File is éXYZ (c3 a9 58 59 5a). The REST tail covered through é. The
  // stream window started on the continuation byte, so Core already replaced
  // it. Indexing the re-encoded string drops YZ.
  const entries = [tail("active.stdout", "é", 0, 2)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 1,
    next_offset: 5,
    data: "\uFFFDXYZ",
  });
  assert.deepEqual(visible, { offset: 1, nextOffset: 5, data: "\uFFFDXYZ" });
});

test("boundary-safe text bounds keep the suffix after a split multibyte character", () => {
  const entries = [tail("active.stdout", "é", 0, 2)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 1,
    next_offset: 5,
    text_offset: 2,
    text_next_offset: 5,
    data: "XYZ",
  });
  assert.deepEqual(visible, { offset: 2, nextOffset: 5, data: "XYZ" });
});

test("a faithful cut inside a multibyte character keeps only complete new characters", () => {
  // "caféXYZ" is 8 bytes; byte 4 is the second byte of é.
  const entries = [tail("active.stdout", "caf", 0, 4)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 8,
    data: "caféXYZ",
  });
  assert.deepEqual(visible, { offset: 5, nextOffset: 8, data: "XYZ" });
});

test("text already inside the snapshot is dropped even when the raw window continues", () => {
  const entries = [tail("active.stdout", "hello!!!!!", 0, 10)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 12,
    text_offset: 0,
    text_next_offset: 5,
    data: "hello",
  });
  assert.equal(visible, null);
});

test("a covered partial character is not replayed as a replacement", () => {
  // Byte 1 is the second byte of é. The snapshot already owns that character.
  const entries = [tail("active.stdout", "?", 0, 1)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 0,
    next_offset: 2,
    data: "é",
  });
  assert.equal(visible, null);
});

test("a reconnect frame stored before the tail keeps the suffix the snapshot does not contain", () => {
  // The tail read through byte 9 is still in flight, so the reconnect frame
  // is stored whole. When that tail commits, the post-9 suffix must remain.
  const frame = { offset: 4, next_offset: 11, data: "shot\nHi" };
  const visible = visibleLiveLogFrame([], "ws_logs", "active.stdout", frame);
  assert.deepEqual(visible, { offset: 4, nextOffset: 11, data: "shot\nHi" });
  const live = {
    kind: "live",
    key: `live:ws_logs:active.stdout:${visible.offset}:${visible.nextOffset}:3`,
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: visible.offset,
    data: visible.data,
    source: "agent",
  };
  const other = {
    kind: "live",
    key: "live:ws_logs:other.stdout:0:3:1",
    workspaceId: "ws_logs",
    streamId: "other.stdout",
    offset: 0,
    data: "abc",
  };
  const previousTail = tail("active.stdout", "snap", 0, 4);
  const snapshot = tail("active.stdout", "snapshot\n", 0, 9);
  const merged = entriesAfterTailSnapshot([previousTail, other, live], snapshot);
  assert.equal(merged[0], other);
  assert.equal(merged[1].source, "agent");
  assert.deepEqual(
    { kind: merged[1].kind, key: merged[1].key, offset: merged[1].offset, data: merged[1].data },
    {
      kind: "live",
      key: "live:ws_logs:active.stdout:9:11:3",
      offset: 9,
      data: "Hi",
    },
  );
  assert.equal(merged[2], snapshot);
});

test("a live entry the tail already covers is dropped, and one that starts at the boundary stays", () => {
  const covered = {
    kind: "live",
    key: "live:ws_logs:active.stdout:0:9:1",
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: 0,
    data: "snapshot\n",
  };
  const boundary = {
    kind: "live",
    key: "live:ws_logs:active.stdout:9:11:2",
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: 9,
    data: "Hi",
  };
  const merged = entriesAfterTailSnapshot(
    [covered, boundary],
    tail("active.stdout", "snapshot\n", 0, 9),
  );
  assert.equal(merged.length, 2);
  assert.equal(merged[0], boundary);
  assert.equal(merged[1].kind, "tail");
});

test("tail commit uses the stored byte end, not JavaScript string length, and does not slice a lossy frame", () => {
  const multibyte = {
    kind: "live",
    key: "live:ws_logs:active.stdout:0:8:1",
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: 0,
    data: "caféXYZ",
  };
  const lossy = {
    kind: "live",
    key: "live:ws_logs:active.stdout:1:5:2",
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: 1,
    data: "\uFFFDXYZ",
  };
  const cafe = entriesAfterTailSnapshot([multibyte], tail("active.stdout", "café", 0, 5));
  assert.deepEqual(
    { offset: cafe[0].offset, data: cafe[0].data },
    { offset: 5, data: "XYZ" },
  );
  const kept = entriesAfterTailSnapshot([lossy], tail("active.stdout", "é", 0, 2));
  assert.equal(kept[0], lossy);
});

test("a live entry without a byte-end key is trimmed by its UTF-8 length", () => {
  const live = {
    kind: "live",
    key: "live-without-end",
    workspaceId: "ws_logs",
    streamId: "active.stdout",
    offset: 0,
    data: "abYZ",
  };
  const merged = entriesAfterTailSnapshot([live], tail("active.stdout", "ab", 0, 2));
  assert.equal(merged[0].key, "live-without-end");
  assert.deepEqual(
    { offset: merged[0].offset, data: merged[0].data },
    { offset: 2, data: "YZ" },
  );
});

test("bounds that do not match the text are not used as a slice index", () => {
  const entries = [tail("active.stdout", "é", 0, 2)];
  const visible = visibleLiveLogFrame(entries, "ws_logs", "active.stdout", {
    offset: 1,
    next_offset: 5,
    text_offset: 2,
    text_next_offset: 5,
    data: "\uFFFDXYZ",
  });
  assert.deepEqual(visible, { offset: 1, nextOffset: 5, data: "\uFFFDXYZ" });
});
