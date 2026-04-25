import { NextResponse } from "next/server";
import WebSocket from "ws";

export const runtime = "nodejs";

export function awfBaseUrl(): string {
  return (process.env.AWF_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
}

export function awfToken(): string | undefined {
  return process.env.AWF_API_TOKEN?.trim() || undefined;
}

export function awfWebSocketUrl(path: string): string {
  const target = new URL(path, `${awfBaseUrl()}/`);
  target.protocol = target.protocol === "https:" ? "wss:" : "ws:";
  return target.toString();
}

export async function proxyAwfGet(path: string): Promise<NextResponse> {
  try {
    const response = await fetch(`${awfBaseUrl()}${path}`, {
      cache: "no-store",
      headers: awfHeaders(),
    });
    const text = await response.text();
    return new NextResponse(text, {
      status: response.status,
      headers: {
        "content-type": response.headers.get("content-type") || "application/json",
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    return NextResponse.json(
      normalizeError(error, "AWF_API_UNREACHABLE", "Unable to reach the AWF API."),
      { status: 502 },
    );
  }
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
