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
