import { createServer, type ServerResponse } from "node:http";
import { test as base } from "@playwright/test";
import type { AwfStreamFrame } from "@/lib/types";

// Unlike route.fulfill(), this fixture leaves the SSE connection open so a
// live-status assertion exercises a real EventSource, including reconnects.
export const streamTest = base.extend<{
  openEventStream: (frames: AwfStreamFrame[]) => Promise<string>;
}>({
  openEventStream: async ({}, provide) => {
    const servers: ReturnType<typeof createServer>[] = [];
    const responses = new Set<ServerResponse>();
    try {
      await provide(async (frames) => {
        const server = createServer((_request, response) => {
          response.writeHead(200, {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
            "access-control-allow-origin": "*",
          });
          responses.add(response);
          response.on("close", () => responses.delete(response));
          for (const frame of frames) {
            response.write(`data: ${JSON.stringify(frame)}\n\n`);
          }
        });
        servers.push(server);
        await new Promise<void>((resolve, reject) => {
          server.once("error", reject);
          server.listen(0, "127.0.0.1", resolve);
        });
        const address = server.address();
        if (!address || typeof address === "string") throw new Error("SSE fixture did not bind");
        return `http://127.0.0.1:${address.port}/stream`;
      });
    } finally {
      for (const response of responses) response.destroy();
      await Promise.all(servers.map((server) => new Promise<void>((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      })));
    }
  },
});
