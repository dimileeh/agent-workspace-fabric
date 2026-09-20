export type TailCoverageEntry = {
  kind: "tail" | "live";
  key: string;
  workspaceId: string;
  streamId: string;
};

export type LiveLogFrameView = {
  offset: number;
  nextOffset: number;
  data: string;
};

type LiveLogFrame = {
  offset: number;
  next_offset?: number;
  text_offset?: number;
  text_next_offset?: number;
  data: string;
};

type TextByteRange = {
  textOffset: number;
  textEnd: number;
};

/**
 * Successful tail keys end with `:${offset}:${nextOffset}`. nextOffset is the
 * server's UTF-8 byte end. Denial diagnostics are not coverage, and
 * data.length is not either: a multibyte snapshot is shorter in JavaScript
 * than the byte range it already painted.
 */
function tailSnapshotByteEnd(entry: TailCoverageEntry): number | null {
  if (entry.kind !== "tail" || entry.key.startsWith("tail-error:")) {
    return null;
  }
  const match = /:(\d+):(\d+)$/.exec(entry.key);
  if (match === null) {
    return null;
  }
  return Number(match[2]);
}

export function coveredTailByteEnd(
  entries: readonly TailCoverageEntry[],
  workspaceId: string,
  streamId: string,
): number {
  let covered = 0;
  for (const entry of entries) {
    if (entry.workspaceId !== workspaceId || entry.streamId !== streamId) {
      continue;
    }
    const end = tailSnapshotByteEnd(entry);
    if (end !== null) {
      covered = Math.max(covered, end);
    }
  }
  return covered;
}

function frameByteEnd(frame: LiveLogFrame): number {
  if (typeof frame.next_offset === "number") {
    return frame.next_offset;
  }
  return frame.offset + new TextEncoder().encode(frame.data).length;
}

/**
 * Span whose UTF-8 encoding is exactly `encodedLength`.
 *
 * `text_offset` / `text_next_offset` are the server's boundary-safe range.
 * Without them, the raw window is safe only when re-encoding `data` does not
 * change its length. A replacement character from a mid-sequence decode is
 * three bytes, so `covered - offset` would no longer name the file range.
 */
function boundarySafeTextRange(
  frame: LiveLogFrame,
  frameEnd: number,
  encodedLength: number,
): TextByteRange | null {
  if (typeof frame.text_offset === "number" && typeof frame.text_next_offset === "number") {
    const span = frame.text_next_offset - frame.text_offset;
    if (encodedLength !== span) {
      return null;
    }
    return { textOffset: frame.text_offset, textEnd: frame.text_next_offset };
  }
  const span = frameEnd - frame.offset;
  if (encodedLength !== span) {
    return null;
  }
  return { textOffset: frame.offset, textEnd: frameEnd };
}

function utf8IndexAtOrAfterCodePoint(bytes: Uint8Array, index: number): number {
  let cursor = index;
  while (cursor < bytes.length && (bytes[cursor] & 0xc0) === 0x80) {
    cursor += 1;
  }
  return cursor;
}

/**
 * Drop a reconnect replay that the tail snapshot already painted. A frame that
 * starts inside that byte range and continues past it keeps only the suffix
 * when the text is a faithful encoding of a known byte span. Discarding the
 * whole frame would hide lines written after the snapshot, and indexing a
 * replacement decode would corrupt that suffix.
 */
export function visibleLiveLogFrame(
  entries: readonly TailCoverageEntry[],
  workspaceId: string,
  streamId: string,
  frame: LiveLogFrame,
): LiveLogFrameView | null {
  const covered = coveredTailByteEnd(entries, workspaceId, streamId);
  const frameEnd = frameByteEnd(frame);
  if (frameEnd <= covered || frameEnd <= frame.offset) {
    return null;
  }
  if (frame.offset >= covered) {
    return { offset: frame.offset, nextOffset: frameEnd, data: frame.data };
  }
  const bytes = new TextEncoder().encode(frame.data);
  const range = boundarySafeTextRange(frame, frameEnd, bytes.length);
  if (range === null) {
    return { offset: frame.offset, nextOffset: frameEnd, data: frame.data };
  }
  if (range.textEnd <= covered) {
    return null;
  }
  if (range.textOffset >= covered) {
    return { offset: range.textOffset, nextOffset: frameEnd, data: frame.data };
  }
  const start = utf8IndexAtOrAfterCodePoint(bytes, covered - range.textOffset);
  if (start >= bytes.length) {
    return null;
  }
  const data = new TextDecoder().decode(bytes.subarray(start));
  return { offset: range.textOffset + start, nextOffset: frameEnd, data };
}

type ReplayLogEntry = {
  kind: "tail" | "live";
  key: string;
  workspaceId: string;
  streamId: string;
  offset: number;
  data: string;
};

function storedLiveFrame(entry: ReplayLogEntry): LiveLogFrame {
  const match = /:(\d+):(\d+):(\d+)$/.exec(entry.key);
  if (match === null) {
    return { offset: entry.offset, data: entry.data };
  }
  return {
    offset: entry.offset,
    next_offset: Number(match[2]),
    data: entry.data,
  };
}

function entryWithVisibleSuffix<T extends ReplayLogEntry>(entry: T, visible: LiveLogFrameView): T {
  if (visible.offset === entry.offset && visible.data === entry.data) {
    return entry;
  }
  return {
    ...entry,
    key: entry.key.replace(/:(\d+):(\d+):(\d+)$/, `:${visible.offset}:${visible.nextOffset}:$3`),
    offset: visible.offset,
    data: visible.data,
  };
}

/**
 * Commit a tail snapshot that may land after a reconnect frame was stored whole.
 * A frame that starts inside the snapshot and continues past it keeps the
 * uncovered suffix. Dropping every live entry whose start is below the
 * snapshot end hides bytes the snapshot does not contain.
 */
export function entriesAfterTailSnapshot<T extends ReplayLogEntry>(
  entries: readonly T[],
  snapshot: T,
): T[] {
  const retained: T[] = [];
  for (const entry of entries) {
    if (entry.workspaceId !== snapshot.workspaceId || entry.streamId !== snapshot.streamId) {
      retained.push(entry);
      continue;
    }
    if (entry.kind !== "live") {
      continue;
    }
    const visible = visibleLiveLogFrame(
      [snapshot],
      entry.workspaceId,
      entry.streamId,
      storedLiveFrame(entry),
    );
    if (visible === null) {
      continue;
    }
    retained.push(entryWithVisibleSuffix(entry, visible));
  }
  retained.push(snapshot);
  return retained;
}

/** Advance the stream cursor only when the frame contains bytes past it. */
export function streamOffsetAfterLiveFrame(
  known: number,
  frame: { offset: number; next_offset?: number },
): number {
  const frameEnd = frame.next_offset ?? frame.offset;
  return frameEnd > known ? frameEnd : known;
}
