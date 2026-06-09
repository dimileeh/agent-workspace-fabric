import { createServer } from "node:http";

const FIXTURE_ID = "awf-node-profile-fixture";
let setupComplete = false;

function sendText(response, body, status = 200, contentType = "text/plain; charset=utf-8") {
  const payload = Buffer.from(body, "utf8");
  response.writeHead(status, {
    "Content-Type": contentType,
    "Content-Length": String(payload.length),
  });
  response.end(payload);
}

function sendJson(response, value, status = 200) {
  sendText(response, `${JSON.stringify(value)}\n`, status, "application/json; charset=utf-8");
}

function rootPage() {
  const ready = setupComplete ? "true" : "false";
  const status = setupComplete ? "setup complete" : "setup pending";
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>AWF Node Profile Fixture</title>
  </head>
  <body>
    <main data-awf-ready="${ready}" data-fixture-id="${FIXTURE_ID}">
      <h1>AWF Node Profile Fixture</h1>
      <p id="status">${status}</p>
    </main>
  </body>
</html>
`;
}

const server = createServer((request, response) => {
  if (request.method !== "GET") {
    sendText(response, "method not allowed\n", 405);
    return;
  }

  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (url.pathname === "/healthz") {
    sendText(response, "ok\n");
    return;
  }
  if (url.pathname === "/setup") {
    setupComplete = true;
    sendText(response, "setup ok\n");
    return;
  }
  if (url.pathname === "/api/status") {
    sendJson(response, {
      id: FIXTURE_ID,
      ready: setupComplete,
      runtime: "node-next-browser-app",
    });
    return;
  }
  if (url.pathname === "/") {
    sendText(response, rootPage(), 200, "text/html; charset=utf-8");
    return;
  }

  sendText(response, "not found\n", 404);
});

const port = Number.parseInt(process.env.PORT ?? "3000", 10);
server.listen(port, "0.0.0.0");
