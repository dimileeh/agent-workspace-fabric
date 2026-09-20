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
  textOffset?: number;
  textNextOffset?: number;
};

function textSpanForData(
  offset: number,
  data: string,
  knownEnd?: number,
): { textOffset: number; textNextOffset: number } {
  const encodedEnd = offset + new TextEncoder().encode(data).length;
  // A suffix cut from a boundary-aligned frame must keep that frame's text
  // end. Re-encoding agrees with it for a faithful slice; any other known end
  // does not describe `data` and cannot be used as the stored span.
  return {
    textOffset: offset,
    textNextOffset: knownEnd === encodedEnd ? knownEnd : encodedEnd,
  };
}

/**
 * Bounds that still name `visible.data`.
 *
 * Server text bounds describe the original payload. A suffix trimmed before
 * the entry is stored is a different span, and inventing bounds for an
 * untrimmed payload would hide a lossy window that must not be sliced.
 */
export function textBoundsForStoredLiveEntry(
  frame: { data: string; text_offset?: number; text_next_offset?: number },
  visible: LiveLogFrameView,
): { textOffset: number; textNextOffset: number } | null {
  if (visible.data !== frame.data) {
    return textSpanForData(visible.offset, visible.data);
  }
  if (typeof frame.text_offset !== "number" || typeof frame.text_next_offset !== "number") {
    return null;
  }
  return { textOffset: frame.text_offset, textNextOffset: frame.text_next_offset };
}

function storedLiveFrame(entry: ReplayLogEntry): LiveLogFrame {
  const match = /:(\d+):(\d+):(\d+)$/.exec(entry.key);
  const frame: LiveLogFrame =
    match === null
      ? { offset: entry.offset, data: entry.data }
      : {
          offset: entry.offset,
          next_offset: Number(match[2]),
          data: entry.data,
        };
  if (typeof entry.textOffset === "number" && typeof entry.textNextOffset === "number") {
    frame.text_offset = entry.textOffset;
    frame.text_next_offset = entry.textNextOffset;
  }
  return frame;
}

function entryWithVisibleSuffix<T extends ReplayLogEntry>(entry: T, visible: LiveLogFrameView): T {
  if (visible.offset === entry.offset && visible.data === entry.data) {
    return entry;
  }
  const bounds = textSpanForData(visible.offset, visible.data, entry.textNextOffset);
  return {
    ...entry,
    key: entry.key.replace(/:(\d+):(\d+):(\d+)$/, `:${visible.offset}:${visible.nextOffset}:$3`),
    offset: visible.offset,
    data: visible.data,
    textOffset: bounds.textOffset,
    textNextOffset: bounds.textNextOffset,
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

/**
 * Advance the stream cursor only when the frame contains bytes past it.
 * Live frames omit next_offset. Their data is the redacted string appended
 * to the log, so the end is offset plus that string's UTF-8 byte length.
 * Frames that already carry next_offset, including replacement-decoded
 * tails, keep that end instead.
 */
export function streamOffsetAfterLiveFrame(
  known: number,
  frame: { offset: number; next_offset?: number; data: string },
): number {
  const frameEnd = frameByteEnd(frame);
  return frameEnd > known ? frameEnd : known;
}

/**
 * Keep the displayed byte cursor monotonic when a tail snapshot commits.
 * A live frame may already have advanced past this read while the response
 * was still pending. Writing the snapshot end unconditionally would rewind
 * the cursor even though the post-snapshot live suffix stays visible.
 */
export function streamOffsetAfterTailSnapshot(known: number, snapshotEnd: number): number {
  return snapshotEnd > known ? snapshotEnd : known;
}
