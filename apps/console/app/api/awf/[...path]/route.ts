import { NextRequest } from "next/server";
import {
  encodeSse,
  normalizeError,
  openAwfWorkspaceSocket,
  proxyAwf,
  proxyAwfGet,
  sanitizeStreamChannels,
  sanitizeTailBytes,
} from "@/lib/awf-server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ path?: string[] }>;
};

export async function GET(request: NextRequest, context: RouteContext) {
  const { path = [] } = await context.params;

  if (path[0] === "health") {
    return proxyAwfGet("/healthz");
  }

  if (path[0] === "workspaces" && path[1] && path[2] === "stream") {
    return streamWorkspace(request, path[1]);
  }

  const awfPath = `/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  return proxyAwfGet(awfPath);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyAwfWrite(request, context, "POST");
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxyAwfWrite(request, context, "DELETE");
}

async function proxyAwfWrite(
  request: NextRequest,
  context: RouteContext,
  method: "POST" | "DELETE",
): Promise<Response> {
  const { path = [] } = await context.params;
  const awfPath = `/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const body = await request.text();

  return proxyAwf(awfPath, {
    method,
    body: body || undefined,
    contentType: request.headers.get("content-type"),
  });
}

async function streamWorkspace(request: NextRequest, workspaceId: string): Promise<Response> {
  const channels = sanitizeStreamChannels(request.nextUrl.searchParams.get("channels"));
  const tailBytes = sanitizeTailBytes(request.nextUrl.searchParams.get("tail_bytes"));

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      let closed = false;
      const send = (payload: unknown, event?: string) => {
        if (closed) {
          return;
        }
        controller.enqueue(encoder.encode(encodeSse(payload, event)));
      };

      const close = () => {
        if (!closed) {
          closed = true;
          controller.close();
        }
      };

      const socket = openAwfWorkspaceSocket({ workspaceId, channels, tailBytes });
      let socketOpened = false;

      socket.on("open", () => {
        socketOpened = true;
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
        send({
          type: "error",
          ...normalizeError(error, "AWF_STREAM_ERROR", "AWF workspace stream failed."),
        });
        if (!socketOpened) {
          close();
        }
      });
      socket.on("close", (code, reason) => {
        send(
          {
            type: "closed",
            workspace_id: workspaceId,
            code,
            reason: reason.toString("utf-8"),
          },
        );
        close();
      });

      request.signal.addEventListener("abort", () => {
        socket.close();
        close();
      });
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
