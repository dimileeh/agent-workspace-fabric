import {
  consoleApiFetch,
  isConsoleSessionExpired,
  subscribeToConsoleSessionExpiry,
} from "./console-api-session.ts";

export type ConsoleEventStream = {
  readonly closed: boolean;
  close: () => void;
  onmessage: ((message: { data: string }) => void) | null;
  onerror: (() => void) | null;
};

// EventSource hides the HTTP handshake status. Read the SSE response through
// the console request boundary so a 401 also ends the session on this path.
export function openConsoleEventStream(path: string): ConsoleEventStream {
  const controller = new AbortController();
  let retryDelay = 3000;
  let lastEventId = "";
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  const unsubscribe = subscribeToConsoleSessionExpiry(close);
  const stream: ConsoleEventStream = {
    get closed() { return controller.signal.aborted; },
    close,
    onmessage: null,
    onerror: null,
  };

  function close() {
    controller.abort();
    clearTimeout(retryTimer);
    unsubscribe();
  }

  async function read(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const lineEnd = /\r\n?|\n/g;
    let buffer = "";
    let event = "";
    let data: string[] = [];
    try {
      while (!stream.closed) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value, { stream: !chunk.done });
        lineEnd.lastIndex = 0;
        let start = 0;
        let match: RegExpExecArray | null;
        while (!stream.closed && (match = lineEnd.exec(buffer))) {
          // A CR at a chunk boundary may be the first half of CRLF.
          if (!chunk.done && match[0] === "\r" && lineEnd.lastIndex === buffer.length) break;
          const line = buffer.slice(start, match.index);
          start = lineEnd.lastIndex;
          if (line === "") {
            if (data.length > 0 && (event === "" || event === "message")) {
              stream.onmessage?.({ data: data.join("\n") });
            }
            event = "";
            data = [];
          } else if (!line.startsWith(":")) {
            const separator = line.indexOf(":");
            const field = separator < 0 ? line : line.slice(0, separator);
            const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
            if (field === "data") data.push(value);
            if (field === "event") event = value;
            if (field === "id" && !value.includes("\0")) lastEventId = value;
            if (field === "retry" && /^\d+$/.test(value)) {
              retryDelay = Math.min(Number(value), 2_147_483_647);
            }
          }
        }
        if (chunk.done) return;
        buffer = buffer.slice(start);
      }
    } finally {
      reader.releaseLock();
    }
  }

  async function connect() {
    try {
      const headers = new Headers({ accept: "text/event-stream" });
      if (lastEventId) headers.set("last-event-id", lastEventId);
      const response = await consoleApiFetch(path, {
        cache: "no-store",
        credentials: "same-origin",
        headers,
        signal: controller.signal,
      });
      const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
      if (response.status !== 200 || contentType !== "text/event-stream" || !response.body) {
        close();
        stream.onerror?.();
        return;
      }
      await read(response.body);
    } catch {
      // Network interruptions reconnect. The abort signal distinguishes
      // intentional cleanup and session expiration from an interrupted stream.
    }
    if (!stream.closed) {
      stream.onerror?.();
      if (!stream.closed) retryTimer = setTimeout(() => { void connect(); }, retryDelay);
    }
  }

  if (isConsoleSessionExpired()) close();
  else queueMicrotask(() => {
    // Let effect cleanup discard a superseded stream before starting its fetch.
    if (!stream.closed) void connect();
  });
  return stream;
}
