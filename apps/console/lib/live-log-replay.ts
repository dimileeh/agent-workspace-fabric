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
  data: string;
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
 * Drop a reconnect replay that the tail snapshot already painted. A frame that
 * starts inside that byte range and continues past it keeps only the suffix;
 * discarding the whole frame would hide lines written after the snapshot.
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
  const start = covered - frame.offset;
  const end = Math.min(bytes.length, frameEnd - frame.offset);
  if (end <= start) {
    return null;
  }
  const data = new TextDecoder().decode(bytes.subarray(start, end));
  if (data.length === 0) {
    return null;
  }
  return { offset: covered, nextOffset: frameEnd, data };
}

/** Advance the stream cursor only when the frame contains bytes past it. */
export function streamOffsetAfterLiveFrame(
  known: number,
  frame: { offset: number; next_offset?: number },
): number {
  const frameEnd = frame.next_offset ?? frame.offset;
  return frameEnd > known ? frameEnd : known;
}
