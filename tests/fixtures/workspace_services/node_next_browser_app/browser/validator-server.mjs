import { createServer } from "node:http";
import { chromium } from "playwright-core";

const FIXTURE_ID = "awf-node-profile-fixture";
let browserPromise;

function sendText(response, body, status = 200) {
  const payload = Buffer.from(body, "utf8");
  response.writeHead(status, {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": String(payload.length),
  });
  response.end(payload);
}

function requireAppBaseUrl() {
  const value = process.env.APP_BASE_URL;
  if (!value) {
    throw new Error("APP_BASE_URL is required");
  }
  return value;
}

async function validateInBrowser() {
  const browser = await browserForValidation();
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.goto(requireAppBaseUrl(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector('main[data-awf-ready="true"][data-fixture-id="awf-node-profile-fixture"]', {
      timeout: 10_000,
    });

    const heading = await page.textContent("h1");
    if (heading !== "AWF Node Profile Fixture") {
      throw new Error(`unexpected heading: ${JSON.stringify(heading)}`);
    }

    const status = await page.evaluate(async () => {
      const response = await fetch("/api/status");
      return response.json();
    });
    if (
      status.id !== FIXTURE_ID ||
      status.ready !== true ||
      status.runtime !== "node-next-browser-app"
    ) {
      throw new Error(`unexpected status payload: ${JSON.stringify(status)}`);
    }
  } finally {
    await context.close();
  }
}

function launchBrowser() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim();
  const promise = chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
    ...(executablePath ? { executablePath } : {}),
  }).catch((error) => {
    if (browserPromise === promise) {
      browserPromise = undefined;
    }
    throw error;
  });

  browserPromise = promise;
  return promise;
}

async function browserForValidation() {
  const current = browserPromise ?? launchBrowser();
  const browser = await current;
  if (browser.isConnected()) {
    return browser;
  }

  if (browserPromise === current) {
    browserPromise = undefined;
  }
  return browserPromise ?? launchBrowser();
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
  if (url.pathname === "/validate") {
    validateInBrowser()
      .then(() => {
        sendText(response, "browser validated awf-node-profile-fixture\n");
      })
      .catch((error) => {
        sendText(response, `browser validation failed: ${error.message}\n`, 500);
      });
    return;
  }

  sendText(response, "not found\n", 404);
});

const port = Number.parseInt(process.env.PORT ?? "9323", 10);
const host = process.env.AWF_VALIDATOR_HOST ?? "0.0.0.0";
server.listen(port, host);
