import { NextResponse } from "next/server.js";
import WebSocket from "ws";

export const runtime = "nodejs";

const defaultAwfFetchTimeoutMs = 30_000;

export function awfBaseUrl(): string {
  return (process.env.AWF_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
}

export function awfToken(): string | undefined {
  return process.env.AWF_API_TOKEN?.trim() || undefined;
}

export function awfFetchTimeoutMs(): number {
  const raw = process.env.AWF_API_FETCH_TIMEOUT_MS?.trim();
  if (!raw) {
    return defaultAwfFetchTimeoutMs;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : defaultAwfFetchTimeoutMs;
}

export function awfWebSocketUrl(path: string): string {
  const target = new URL(path, `${awfBaseUrl()}/`);
  target.protocol = target.protocol === "https:" ? "wss:" : "ws:";
  return target.toString();
}

type AwfProxyMethod = "GET" | "POST" | "DELETE";

type AwfProxyOptions = {
  method?: AwfProxyMethod;
  body?: string;
  contentType?: string | null;
  headers?: Record<string, string | undefined>;
};

export async function proxyAwf(path: string, options: AwfProxyOptions = {}): Promise<NextResponse> {
  const { method = "GET", body, contentType } = options;

  try {
    const headers = awfHeaders();
    if (body !== undefined && contentType) {
      headers["content-type"] = contentType;
    }
    for (const [key, value] of Object.entries(options.headers ?? {})) {
      if (!value) {
        continue;
      }
      const normalized = key.toLowerCase();
      if (normalized === "authorization" || normalized === "cookie") {
        continue;
      }
      headers[key] = value;
    }

    const timeoutMs = awfFetchTimeoutMs();
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      controller.abort(new Error(`AWF API request timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    try {
      const response = await fetch(`${awfBaseUrl()}${path}`, {
        method,
        cache: "no-store",
        headers,
        body,
        signal: controller.signal,
      });
      const text = await response.text();
      return new NextResponse(text, {
        status: response.status,
        headers: {
          "content-type": response.headers.get("content-type") || "application/json",
          "cache-control": "no-store",
        },
      });
    } finally {
      clearTimeout(timeout);
    }
  } catch (error) {
    return NextResponse.json(
      normalizeError(error, "AWF_API_UNREACHABLE", "Unable to reach the AWF API."),
      { status: 502 },
    );
  }
}

export async function proxyAwfGet(path: string): Promise<NextResponse> {
  return proxyAwf(path);
}

export function awfHeaders(): Record<string, string> {
  const token = awfToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

export function normalizeError(error: unknown, errorCode: string, message: string) {
  return {
    ok: false,
    error_code: errorCode,
    message,
    detail: error instanceof Error ? error.message : String(error),
  };
}

export function encodeSse(data: unknown, event = "message"): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

export const AWF_STREAM_CHANNELS = ["events", "agent", "validation", "services"] as const;
const AWF_STREAM_CHANNEL_SET = new Set<string>(AWF_STREAM_CHANNELS);
const AWF_STREAM_DEFAULT_CHANNELS = AWF_STREAM_CHANNELS.join(",");
export const AWF_STREAM_MAX_TAIL_BYTES = 65_536;

export function sanitizeStreamChannels(raw: string | null): string {
  const filtered = (raw ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter((value) => AWF_STREAM_CHANNEL_SET.has(value));
  return filtered.length > 0 ? filtered.join(",") : AWF_STREAM_DEFAULT_CHANNELS;
}

export function sanitizeTailBytes(raw: string | null): string {
  const trimmed = (raw ?? "").trim();
  const parsed = Number(trimmed || AWF_STREAM_MAX_TAIL_BYTES);
  if (!Number.isFinite(parsed)) {
    return String(AWF_STREAM_MAX_TAIL_BYTES);
  }
  const clamped = Math.min(Math.max(Math.trunc(parsed), 0), AWF_STREAM_MAX_TAIL_BYTES);
  return String(clamped);
}

export function openAwfWorkspaceSocket({
  workspaceId,
  channels,
  tailBytes,
}: {
  workspaceId: string;
  channels: string;
  tailBytes: string;
}): WebSocket {
  const params = new URLSearchParams({
    channels,
    tail_bytes: tailBytes,
  });
  return new WebSocket(
    awfWebSocketUrl(`/v1/workspaces/${encodeURIComponent(workspaceId)}/ws?${params}`),
    {
      headers: awfHeaders(),
    },
  );
}

type WorkspaceStreamSocket = Pick<WebSocket, "on" | "close">;

/**
 * Wire an upstream workspace WebSocket into an SSE stream.
 *
 * A stream error is terminal: after surfacing the error frame we close the
 * upstream socket and end the SSE response deterministically, rather than
 * relying on a follow-up "close" event firing. Previously a mid-stream error
 * (socket already opened) only emitted an error frame and left the connection
 * dangling until the upstream happened to also emit "close".
 */
export function attachWorkspaceStreamHandlers({
  socket,
  workspaceId,
  send,
  closeStream,
}: {
  socket: WorkspaceStreamSocket;
  workspaceId: string;
  send: (payload: unknown, event?: string) => void;
  closeStream: () => void;
}): void {
  // Both "error" and "close" run terminal logic, and our own socket.close() in
  // the error path provokes a follow-up "close". Guard terminal handling inside
  // the helper so it stays deterministic (exactly one terminal frame + one
  // closeStream) without relying on caller idempotency.
  let terminated = false;

  socket.on("open", () => {
    send({ type: "connected", workspace_id: workspaceId });
  });
  socket.on("message", (data) => {
    const text = typeof data === "string" ? data : data.toString("utf-8");
    try {
      send(JSON.parse(text));
    } catch {
      send({ type: "raw", data: text });
    }
  });
  socket.on("error", (error) => {
    if (terminated) {
      return;
    }
    terminated = true;
    send({
      type: "error",
      ...normalizeError(error, "AWF_STREAM_ERROR", "AWF workspace stream failed."),
    });
    socket.close();
    closeStream();
  });
  socket.on("close", (code, reason) => {
    if (terminated) {
      return;
    }
    terminated = true;
    send({
      type: "closed",
      workspace_id: workspaceId,
      code,
      reason: reason.toString("utf-8"),
    });
    closeStream();
  });
}
