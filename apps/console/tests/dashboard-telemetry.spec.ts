import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fulfillJson, hostedTelemetryCapabilities, localCapabilities, mockAwfConsoleApi } from "./fixtures/console-api";

const fixture = (name = "unpriced_allocation") => JSON.parse(readFileSync(`${process.cwd()}/lib/fixtures/console-workspace-telemetry/persisted_${name}.json`, "utf8"));
const overview = (id: string) => ({ workspace_id: id, title: id, repo_url: "https://github.com/test/repo", base_branch: "main", agent: "test", status: "running", created_at: "2026-09-12T12:00:00Z", updated_at: "2026-09-12T12:00:00Z", lifecycle: [], llm_usage: null, recovery: null });
function enabled() {
  const caps = localCapabilities() as { widgets: Array<Record<string, unknown>> };
  caps.widgets = caps.widgets.map(w => ["telemetry", "allocation", "cost"].includes(String(w.id)) ? { id: w.id, availability: "available", route: "/v1/workspaces/{workspace_id}/telemetry", semantics: "Synthetic selected telemetry" } : w);
  return caps;
}
async function setup(page: Page, active = true) {
  const caps = active ? enabled() : localCapabilities();
  await mockAwfConsoleApi(page, { capabilities: caps, overviewItems: [overview("ws_unpriced_allocation"), overview("ws_other")] });
  await page.route("**/api/awf/workspaces/ws_*", async route => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
    await fulfillJson(route, overview(id));
  });
  return caps;
}
async function open(page: Page, id = "ws_unpriced_allocation") {
  await page.getByRole("button", { name: `Open workspace details for ${id}`, exact: true }).click();
}

test("local unsupported inspector never fetches telemetry", async ({ page }) => {
  await setup(page, false);
  let reads = 0;
  await page.route("**/telemetry?*", async route => { reads++; await fulfillJson(route, fixture()); });
  await page.goto("/"); await open(page);
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
  expect(reads).toBe(0);
});

test("one selected read for all gates; minute cadence and distinct views", async ({ page }) => {
  await setup(page);
  const reads: string[] = [];
  await page.route("**/telemetry?*", async route => {
    const view = new URL(route.request().url()).searchParams.get("view")!;
    reads.push(view); const body = fixture(); body.view = view;
    await fulfillJson(route, body);
  });
  await page.clock.install({ time: new Date("2026-09-12T12:01:00Z") });
  await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  expect(reads).toEqual(["1h"]);
  await page.clock.runFor(59_000); expect(reads).toHaveLength(1);
  await page.clock.runFor(1_100); await expect.poll(() => reads.length).toBe(2);
  await page.getByTestId("telemetry-view-6h").click();
  await expect.poll(() => reads.at(-1)).toBe("6h");
  await expect(page.getByTestId("telemetry-view-24h")).toBeVisible();
  await page.getByTestId("telemetry-view-24h").click();
  await expect.poll(() => reads.at(-1)).toBe("24h");
  await page.getByRole("button", { name: "Close inspector" }).click();
  const count = reads.length; await page.clock.runFor(61_000); expect(reads).toHaveLength(count);
});

for (const deniedStatus of [401, 403]) {
test(`transient error retains same identity; ${deniedStatus} revocation clears`, async ({ page }) => {
  await setup(page); let status = 200;
  await page.route("**/telemetry?*", async route => { await fulfillJson(route, status === 200 ? fixture() : { detail: { error_code: "TEST_ERROR", message: "Unavailable" } }, status); });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  status = 503; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-request-error")).toContainText("Unavailable");
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  status = deniedStatus; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
});

}

test("slow selected response does not block pane close or leak into next workspace", async ({ page }) => {
  await setup(page); let release!: () => void;
  const held = new Promise<void>(r => { release = r; });
  await page.route("**/telemetry?*", async route => { await held; await fulfillJson(route, fixture()); });
  await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-loading")).toBeVisible();
  await page.getByRole("button", { name: "Close inspector" }).click();
  await open(page, "ws_other"); release();
  await expect(page.getByTestId("telemetry-request-error")).toBeVisible();
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
});

for (const width of [1440, 390]) {
  test(`hosted dense day and persisted states render at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const cases = ["day_two_containers", "retained_cleaned_terminal", "shared_unallocated", "stale_series", "cold_absent"];
    const rows = cases.map(name => overview(fixture(name).ownership.workspace_record_id));
    await mockAwfConsoleApi(page, { mode: "hosted", capabilities: hostedTelemetryCapabilities(), overviewItems: rows });
    await page.route(/\/api\/core-console\/workspaces\/[^/?]+(?:\?.*)?$/, async route => {
      const row = rows.find(row => new URL(route.request().url()).pathname.endsWith(row.workspace_id));
      if (row) await fulfillJson(route, row); else await route.fallback();
    });
    await page.route(/\/api\/core-console\/workspaces\/[^/]+\/(runtime|events|operations|logs)(?:\?.*)?$/, async route => {
      const runtime = new URL(route.request().url()).pathname.endsWith("/runtime");
      await fulfillJson(route, runtime ? { status: "running" } : { items: [], has_more: false });
    });
    const requests: URL[] = [];
    await page.route("**/api/core-console/workspaces/*/telemetry?*", async route => {
      const url = new URL(route.request().url()); requests.push(url);
      const name = cases.find(name => url.pathname.includes(fixture(name).ownership.workspace_record_id))!;
      const body = fixture(name);
      // Tenant context is authorized per this synthetic test; exported files stay untouched.
      body.ownership.project_id = "p_test";
      body.view = url.searchParams.get("view");
      if (name === "day_two_containers" && body.view !== "24h") {
        body.cpu_cores_samples = []; body.memory_bytes_samples = [];
      }
      await fulfillJson(route, body);
    });
    await page.goto("http://127.0.0.1:3191/workspaces?org_id=org_synthetic&project_id=p_test");
    for (const name of cases) {
      await open(page, fixture(name).ownership.workspace_record_id);
      await expect(page.getByTestId("console-workspace-telemetry")).toBeVisible();
      if (name === "day_two_containers") {
        await page.getByTestId("telemetry-view-24h").click();
        await expect(page.getByTestId("telemetry-view-24h")).toHaveAttribute("aria-selected", "true");
        await expect(page.getByTestId("telemetry-series-cpu").locator("svg")).toBeVisible();
      }
      if (name === "shared_unallocated") await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unallocated");
      if (name === "cold_absent") await expect(page.getByTestId("telemetry-admission-missing")).toBeVisible();
      await expect.poll(async () => {
        const box = await page.getByTestId("console-workspace-telemetry").boundingBox();
        return box && box.x >= 0 && box.x + box.width <= width;
      }).toBe(true);
      await page.getByRole("button", { name: "Close inspector" }).click();
    }
    expect(requests).toHaveLength(cases.length + 1);
    for (const url of requests) {
      expect(url.searchParams.get("org_id")).toBe("org_synthetic");
      expect(url.searchParams.get("project_id")).toBe("p_test");
      expect([...url.searchParams.keys()].sort()).toEqual(["org_id", "project_id", "view"]);
    }
  });
}

test("individual gates withdraw and stop unsupported polling", async ({ page }) => {
  const caps = await setup(page) as ReturnType<typeof enabled>;
  let reads = 0;
  await page.route("**/telemetry?*", async route => { reads++; await fulfillJson(route, fixture()); });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost")).toBeVisible();
  caps.widgets = caps.widgets.map(w => w.id === "cost" ? { id: "cost", availability: "unsupported", reason_code: "policy_disabled", message: "Disabled by policy", semantics: "disabled" } : w);
  await page.route("**/console/capabilities", route => fulfillJson(route, caps));
  await page.getByRole("button", { name: "Reload workspace" }).click();
  await expect(page.getByTestId("telemetry-workload-cost")).toHaveCount(0);
  await expect(page.getByTestId("telemetry-meter-cpu")).toBeVisible();
  caps.widgets = caps.widgets.map(w => ["telemetry", "allocation"].includes(String(w.id)) ? { id: w.id, availability: "unsupported", reason_code: "policy_disabled", message: "Disabled by policy", semantics: "disabled" } : w);
  await page.getByRole("button", { name: "Reload workspace" }).click();
  await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
  const count = reads; await page.clock.runFor(61_000); expect(reads).toBe(count);
});

test("provider age advances past five minutes while malformed poll retains data", async ({ page }) => {
  await setup(page); let malformed = false;
  await page.route("**/telemetry?*", route => fulfillJson(route, malformed ? { invalid: true } : fixture()));
  await page.clock.install({ time: new Date("2026-09-12T12:04:00Z") });
  await page.goto("/"); await open(page);
  await expect(page.getByTestId("console-workspace-telemetry")).toBeVisible();
  const panel = page.getByTestId("console-workspace-telemetry").locator("xpath=ancestor::section[1]");
  await expect(panel.getByTitle("Showing the last snapshot — live data may be stale")).toHaveCount(0);
  malformed = true; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-request-error")).toContainText("Malformed");
  await expect(panel.getByTitle("Showing the last snapshot — live data may be stale")).toBeVisible();
});

for (const oldStatus of [200, 403]) {
  test(`tenant switch rejects late prior-context ${oldStatus}`, async ({ page }) => {
    await mockAwfConsoleApi(page, { mode: "hosted", capabilities: hostedTelemetryCapabilities(), overviewItems: [overview("ws_unpriced_allocation")] });
    await page.route(/\/api\/core-console\/workspaces\/ws_unpriced_allocation\?/, route => fulfillJson(route, overview("ws_unpriced_allocation")));
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    let finishOld!: () => void;
    const oldDone = new Promise<void>(resolve => { finishOld = resolve; });
    let firstRead = false;
    await page.route("**/telemetry?*", async route => {
      const project = new URL(route.request().url()).searchParams.get("project_id");
      if (project === "old") { firstRead = true; await held; }
      const body = fixture(); body.ownership.project_id = project;
      await fulfillJson(route, body, project === "old" ? oldStatus : 200);
      if (project === "old") finishOld();
    });
    await page.goto("http://127.0.0.1:3191/workspaces?org_id=org_synthetic&project_id=old");
    await open(page); await expect.poll(() => firstRead).toBe(true);
    await page.evaluate(() => history.pushState({}, "", "?org_id=org_synthetic&project_id=new"));
    await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
    await open(page);
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
    release();
    await oldDone;
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  });
}

test("view switch discards late data and clears last-success while loading", async ({ page }) => {
  await setup(page); let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/telemetry?*", async route => {
    const view = new URL(route.request().url()).searchParams.get("view");
    if (view === "6h") await held;
    const body = fixture(); body.view = view; await fulfillJson(route, body);
  });
  await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toBeVisible();
  await page.getByTestId("telemetry-view-6h").click();
  await expect(page.getByTestId("telemetry-loading")).toBeVisible();
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
  await page.getByTestId("telemetry-view-24h").click();
  await expect(page.getByTestId("telemetry-view-selector")).toHaveAttribute("data-awf-displayed-view", "24h");
  release();
  await expect(page.getByTestId("telemetry-view-selector")).toHaveAttribute("data-awf-displayed-view", "24h");
});

test("slow requests time out without overlap and recover on the next minute", async ({ page }) => {
  await setup(page); let reads = 0; let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/telemetry?*", async route => { reads++; if (reads === 1) await held; await fulfillJson(route, fixture()); });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect.poll(() => reads).toBe(1);
  await page.clock.runFor(29_000); expect(reads).toBe(1);
  await page.clock.runFor(2_000);
  await expect(page.getByTestId("telemetry-request-error")).toBeVisible();
  await page.clock.runFor(58_000); expect(reads).toBe(1);
  await page.clock.runFor(2_000);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  expect(reads).toBe(2); release();
});

test("new attempt evidence clears last-success even when its body is malformed", async ({ page }) => {
  await setup(page); let replace = false;
  await page.route("**/telemetry?*", async route => {
    const body = fixture(); if (replace) { body.ownership.placement_attempt = 2; body.estimate.estimated_usd = "garbage"; }
    await fulfillJson(route, body);
  });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  replace = true; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-request-error")).toContainText("Malformed");
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
});

for (const gate of ["telemetry", "allocation", "cost"]) {
  test(`only the ${gate} section is available`, async ({ page }) => {
    const caps = enabled();
    caps.widgets = caps.widgets.map(w => ["telemetry", "allocation", "cost"].includes(String(w.id)) && w.id !== gate
      ? { id: w.id, availability: "unsupported", reason_code: "policy_disabled", message: "Disabled", semantics: "disabled" } : w);
    await mockAwfConsoleApi(page, { capabilities: caps, overviewItems: [overview("ws_unpriced_allocation")] });
    await page.route("**/telemetry?*", route => fulfillJson(route, fixture()));
    await page.goto("/"); await open(page);
    await expect(page.getByTestId("console-workspace-telemetry")).toBeVisible();
    await expect(page.getByTestId("telemetry-series-cpu")).toHaveCount(gate === "telemetry" ? 1 : 0);
    await expect(page.getByTestId("telemetry-workload-cost")).toHaveCount(gate === "cost" ? 1 : 0);
    await expect(page.getByText("Compute class", { exact: true })).toHaveCount(gate === "allocation" ? 1 : 0);
  });
}
