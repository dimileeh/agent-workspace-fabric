import { expect, type Page, type Route } from "@playwright/test";

import { OPERATOR_PREFERENCES_STORAGE_KEY as preferenceKey } from "../lib/operator-preferences";
import { fulfillJson, listEnvelope, localCapabilities, mockAwfConsoleApi } from "./fixtures/console-api";
import { streamTest as test } from "./fixtures/open-event-stream";

const consoleOrigin = "http://127.0.0.1:3190";
const workspaceId = "ws_embedded";
const workspace = {
  workspace_id: workspaceId,
  title: "Embedded test workspace",
  repo_url: "https://github.com/example/app",
  base_branch: "main",
  agent: "codex",
  status: "running",
  created_at: "2026-09-12T12:00:00Z",
  updated_at: "2026-09-12T12:00:00Z",
  lifecycle: [],
};
const openWorkspace = "Open workspace details for Embedded test workspace";
const sessionMessage = { type: "awf-console:unauthenticated" };
const apiPattern = /\/api\/(awf|operator)\//;

async function mockWorkspace(page: Page, options: Parameters<typeof mockAwfConsoleApi>[1] = {}) {
  await mockAwfConsoleApi(page, { overviewItems: [workspace], ...options });
  await page.route(`**/api/awf/workspaces/${workspaceId}**`, async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(workspaceId)) return fulfillJson(route, workspace);
    if (path.endsWith("/runtime")) return fulfillJson(route, { status: "running" });
    return fulfillJson(route, listEnvelope([]));
  });
}

async function addHost(page: Page, origin = consoleOrigin) {
  await page.route(`${origin}/embedded-test-host`, route => route.fulfill({
    contentType: "text/html",
    body: `<!doctype html><body><script>
      window.received = [];
      window.attempted = [];
      window.addEventListener("message", event => {
        if (event.data?.type === "awf-console:unauthenticated") {
          window.received.push({
            data: event.data,
            origin: event.origin,
            fromConsole: event.source === document.querySelector("iframe").contentWindow,
          });
        }
      });
    </script></body>`,
  }));
  await page.goto(`${origin}/embedded-test-host`);
}

async function mountConsole(page: Page, query: string) {
  await page.evaluate(url => {
    const iframe = document.createElement("iframe");
    iframe.style.cssText = "width: 1200px; height: 680px";
    iframe.src = url;
    document.body.append(iframe);
  }, `${consoleOrigin}/${query}`);
  return page.frameLocator("iframe");
}

function captureApiRequests(page: Page) {
  const requests: string[] = [];
  page.on("request", request => {
    const { pathname } = new URL(request.url());
    if (apiPattern.test(pathname)) requests.push(pathname);
  });
  return requests;
}

const denial = { detail: { error_code: "UNAUTHORIZED", message: "Access denied" } };

test("embedded presentation follows host display preferences across workspace navigation", async ({ page }) => {
  await mockWorkspace(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await addHost(page);
  await page.evaluate(key => localStorage.setItem(key, JSON.stringify({
    version: 1, theme: "dark", fontSize: "large", contrast: "high",
  })), preferenceKey);
  const consoleFrame = await mountConsole(page, "?embedded=1");
  const header = consoleFrame.getByRole("banner");
  await expect(header.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
  await expect(header.getByRole("heading", { name: "AWF Console" })).toHaveCount(0);
  await expect(header.locator(".lucide-boxes")).toHaveCount(0);
  await expect(header.getByRole("button", { name: /theme|font|contrast/i })).toHaveCount(0);
  await expect(header.getByText(/^API:/)).toBeVisible();
  await expect(header.getByText(/^Stream:/)).toBeVisible();
  await expect(header.getByText(/refreshed/)).toBeVisible();

  const root = consoleFrame.locator("html");
  await expect(root).toHaveAttribute("data-awf-theme", "dark");
  await expect(root).toHaveAttribute("data-awf-font-size", "large");
  await expect(root).toHaveAttribute("data-awf-contrast", "high");
  await consoleFrame.getByRole("button", { name: openWorkspace }).click();
  const inspectorClose = consoleFrame.getByRole("button", { name: "Close inspector" });
  await expect(inspectorClose).toBeVisible();
  const query = () => root.evaluate(node => node.ownerDocument.location.search);
  await expect.poll(query).toBe(`?embedded=1&workspaceId=${workspaceId}`);

  const preferences = JSON.stringify({ version: 1, theme: "system", fontSize: "standard", contrast: "normal" });
  await page.evaluate(({ key, value }) => localStorage.setItem(key, value), { key: preferenceKey, value: preferences });
  await expect(root).toHaveAttribute("data-awf-theme-mode", "system");
  await expect(root).toHaveAttribute("data-awf-font-size", "standard");
  await expect(root).toHaveAttribute("data-awf-theme", "dark");
  await expect(root).toHaveAttribute("data-awf-contrast", "normal");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(root).toHaveAttribute("data-awf-theme", "light");
  expect(await page.evaluate(key => localStorage.getItem(key), preferenceKey)).toBe(preferences);
  await page.evaluate(key => localStorage.removeItem(key), preferenceKey);
  await expect(root).toHaveAttribute("data-awf-theme-mode", "light");
  expect(await page.evaluate(key => localStorage.getItem(key), preferenceKey)).toBeNull();

  await inspectorClose.click();
  await expect.poll(query).toBe("?embedded=1");
});

for (const mode of ["embedded frame", "standalone frame", "top-level", "cross-origin frame"] as const) {
  test(`401 stops polling with ${mode} and only notifies a same-origin embedded parent`, async ({ page, openEventStream }) => {
    const requests = captureApiRequests(page);
    await mockWorkspace(page);
    let closed = 0;
    const streamUrl = await openEventStream([{ type: "connected", workspace_id: workspaceId }], () => { closed++; });
    await page.route(`**/workspaces/${workspaceId}/stream?*`, route =>
      route.fulfill({ status: 302, headers: { location: streamUrl } }));
    let held: Route[] | null = null;
    await page.route(/\/api\/awf\/(?:health|console\/capabilities)$/, async route => {
      if (held) held.push(route);
      else await route.fallback();
    });
    await page.clock.install();
    const query = mode === "standalone frame" ? "" : "?embedded=1";
    if (mode === "top-level") {
      await page.addInitScript(() => {
        Object.assign(window, { attempted: [] });
        const original = window.postMessage;
        window.postMessage = function (message: unknown, ...args: unknown[]) {
          if ((message as { type?: string } | null)?.type === "awf-console:unauthenticated") {
            (window as Window & { attempted: unknown[] }).attempted.push(message);
          }
          Reflect.apply(original, this, [message, ...args]);
        };
      });
      await page.goto(`/${query}`);
    } else {
      await addHost(page, mode === "cross-origin frame" ? "http://127.0.0.1:3191" : consoleOrigin);
      if (mode !== "cross-origin frame") {
        await page.addInitScript(() => {
          if (window.parent === window) return;
          const parent = window.parent as Window & { attempted: unknown[] };
          const original = parent.postMessage;
          parent.postMessage = function (data: { type?: string }, ...args: unknown[]) {
            if (data?.type === "awf-console:unauthenticated") {
              parent.attempted.push({ data, targetOrigin: args[0] });
            }
            Reflect.apply(original, parent, [data, ...args]);
          };
        });
      }
      await mountConsole(page, query);
    }
    const consolePage = mode === "top-level" ? page : page.frameLocator("iframe");
    const header = consolePage.getByRole("banner");
    if (mode === "standalone frame") {
      await expect(header.getByRole("heading", { name: "AWF Console" })).toBeVisible();
      await expect(header.getByRole("button", { name: "Use dark theme" })).toBeVisible();
    }
    await consolePage.getByRole("button", { name: openWorkspace }).click();
    await expect(header.getByText("Stream: live", { exact: true })).toBeVisible();
    const polls = () => requests.filter(path => path === "/api/awf/workspaces/overview").length;
    const beforePoll = polls();
    await page.clock.fastForward(6_000);
    await expect.poll(polls).toBeGreaterThan(beforePoll);

    held = [];
    await page.clock.fastForward(6_000);
    await expect.poll(() => held?.length).toBe(2);
    const [first, second] = held;
    await fulfillJson(first, denial, 401);
    await expect(consolePage.getByText("Console session has expired.")).toBeVisible();
    await fulfillJson(second, denial, 401);
    await expect.poll(() => closed).toBe(1);
    const refresh = header.getByRole("button", { name: "Refresh", exact: true });
    await expect(refresh).toBeDisabled();
    await refresh.evaluate(button => button.click());
    const requestCount = requests.length;
    await page.clock.fastForward(125_000);
    await page.clock.fastForward(25_000);
    expect(requests).toHaveLength(requestCount);
    const attempted = () => page.evaluate(() => Reflect.get(window, "attempted"));
    if (mode === "embedded frame") {
      expect(await attempted()).toEqual([{ data: sessionMessage, targetOrigin: consoleOrigin }]);
      await expect.poll(() => page.evaluate(() => Reflect.get(window, "received"))).toEqual([
        { data: sessionMessage, origin: consoleOrigin, fromConsole: true },
      ]);
    } else {
      expect(await attempted()).toEqual([]);
      if (mode !== "top-level") {
        expect(await page.evaluate(() => Reflect.get(window, "received"))).toEqual([]);
      }
    }
  });
}

for (const source of ["telemetry", "stream HTTP", "stream frame 401", "stream frame 403"] as const) {
  test(`${source} denial follows the appropriate shutdown scope`, async ({ page }) => {
    const requests = captureApiRequests(page);
    const capabilities = localCapabilities() as { widgets: Array<Record<string, unknown>> };
    if (source === "telemetry") {
      capabilities.widgets = capabilities.widgets.map(widget => widget.id === "telemetry"
        ? { ...widget, availability: "available", route: "/v1/workspaces/{workspace_id}/telemetry" }
        : widget);
    }
    await mockWorkspace(page, { capabilities });
    await page.route("**/telemetry?*", route => fulfillJson(route, denial, 401));
    await page.route(`**/workspaces/${workspaceId}/stream?*`, async route => {
      if (source === "stream HTTP") return fulfillJson(route, denial, 401);
      const frame = source === "telemetry" ? { type: "closed", workspace_id: workspaceId }
        : { type: "error", workspace_id: workspaceId, status: source.endsWith("403") ? 403 : 401, message: "Stream access denied" };
      await route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify(frame)}\n\n` });
    });
    await page.clock.install();
    await page.goto("/?embedded=1");
    await page.getByRole("button", { name: openWorkspace }).click();
    const refresh = page.getByRole("banner").getByRole("button", { name: "Refresh", exact: true });
    if (source.endsWith("403")) {
      await expect(page.getByText("Stream access denied")).toBeVisible();
      await expect(refresh).toBeEnabled();
      const overview = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/workspaces/overview"));
      await refresh.click();
      await overview;
      return;
    }
    await expect(page.getByText("Console session has expired.")).toBeVisible();
    await expect(refresh).toBeDisabled();
    const count = requests.length;
    await page.clock.fastForward(125_000);
    expect(requests).toHaveLength(count);
  });
}
