import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fulfillJson, hostedTelemetryCapabilities, localCapabilities, mockAwfConsoleApi } from "./fixtures/console-api";

// Each case owns its page, clock, and API mocks. Share the existing worker pool
// so this polling-heavy spec does not leave one worker idle at the end of CI.
test.describe.configure({ mode: "parallel" });

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

test("telemetry timeout reason retains data until the minute retry recovers", async ({ page }) => {
  await setup(page);
  let reads = 0;
  await page.route("**/telemetry?*", async route => {
    reads++;
    if (reads === 2) return; // Hold the second fetch until its deadline aborts it.
    await fulfillJson(route, fixture());
  });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  await page.clock.runFor(60_000);
  await expect.poll(() => reads).toBe(2);
  await page.clock.runFor(30_000);
  await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry request timed out after 30000ms");
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  await page.clock.runFor(59_000);
  expect(reads).toBe(2);
  await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry request timed out after 30000ms");
  await page.clock.runFor(1_000);
  await expect.poll(() => reads).toBe(3);
  await expect(page.getByTestId("telemetry-request-error")).toHaveCount(0);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
});

test("telemetry error codes survive polling retries and clear on recovery", async ({ page }) => {
  await setup(page);
  let status = 200;
  let errorCode = "TELEMETRY_UNAVAILABLE";
  let reads = 0;
  await page.route("**/telemetry?*", async route => {
    reads++;
    await fulfillJson(route, status === 200 ? fixture() : { detail: { error_code: errorCode, message: "Unavailable" } }, status);
  });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  for (const failureStatus of [503, 429]) {
    status = failureStatus;
    errorCode = status === 503 ? "TELEMETRY_UNAVAILABLE" : "RATE_LIMITED";
    for (let retry = 0; retry < 2; retry++) {
      const previousReads = reads;
      await page.clock.runFor(61_000);
      await expect.poll(() => reads).toBe(previousReads + 1);
      await expect(page.getByTestId("telemetry-request-error")).toHaveText(`${errorCode}: Unavailable`);
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
    }
  }
  status = 200;
  await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-request-error")).toHaveCount(0);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
});

for (const deniedStatus of [401, 403]) {
test(`transient error retains same identity; ${deniedStatus} revocation clears`, async ({ page }) => {
  const caps = await setup(page); let status = 200; let reads = 0;
  await page.route("**/telemetry?*", async route => { reads++; await fulfillJson(route, status === 200 ? fixture() : { detail: { error_code: "TEST_ERROR", message: "Unavailable" } }, status); });
  await page.clock.install(); await page.goto("/"); await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  status = 503; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-request-error")).toContainText("Unavailable");
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  status = deniedStatus; await page.clock.runFor(61_000);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
  await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry access denied");
  await expect(page.getByTestId("workspace-card-ws_unpriced_allocation")).toBeAttached();
  await expect(page.getByTestId("workspace-card-ws_other")).toBeAttached();
  await expect(page).toHaveURL(/workspaceId=ws_unpriced_allocation/);
  await expect(page.getByRole("button", { name: "Close inspector" })).toBeVisible();
  const deniedReads = reads;
  let negotiations = 0;
  await page.route("**/console/capabilities", async route => { negotiations++; await fulfillJson(route, caps); });
  await page.getByRole("button", { name: "Reload workspace" }).click();
  await expect.poll(() => negotiations).toBe(1);
  await page.clock.runFor(121_000);
  await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry access denied");
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
  expect(reads).toBe(deniedReads);
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
  await page.route("**/telemetry?*", route => {
    const body = fixture();
    // Retention requires verified ownership even when presentation parsing fails.
    if (malformed) body.estimate.estimated_usd = "garbage";
    return fulfillJson(route, body);
  });
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

for (const ownership of [undefined, null, false, 0, "", [], {}, { workspace_record_id: "ws_other" },
  ...["   ", "\t\n\r"].map(provider_resource_uid => ({ ...fixture().ownership, provider_resource_uid })),
]) {
  test(`invalid ownership ${JSON.stringify(ownership)} rejects initial data and clears last-success`, async ({ page }) => {
    await setup(page);
    let invalid = true;
    await page.route("**/telemetry?*", route => {
      const body = fixture();
      if (invalid) body.ownership = ownership;
      return fulfillJson(route, body);
    });
    await page.clock.install(); await page.goto("/"); await open(page);
    await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry ownership mismatch");
    await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
    invalid = false; await page.clock.runFor(61_000);
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
    invalid = true; await page.clock.runFor(61_000);
    await expect(page.getByTestId("telemetry-request-error")).toHaveText("Telemetry ownership mismatch");
    await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
    invalid = false; await page.clock.runFor(61_000);
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  });
}

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
    const held: import("@playwright/test").Route[] = [];
    await page.route("**/telemetry?*", async route => { held.push(route); });
    await page.clock.install({ time: new Date("2026-09-12T12:10:00Z") });
    await page.goto("/"); await open(page);
    await expect(page.getByTestId("telemetry-loading")).toBeVisible();
    const tabs = page.getByRole("tablist", { name: "Telemetry window" });
    await expect(tabs).toHaveCount(gate === "telemetry" ? 1 : 0);
    await expect.poll(() => held.length).toBe(1);
    await fulfillJson(held[0], { detail: { message: "Unavailable" } }, 503);
    await expect(page.getByTestId("telemetry-request-error")).toContainText("Unavailable");
    await expect(tabs).toHaveCount(gate === "telemetry" ? 1 : 0);
    await page.clock.runFor(61_000);
    await expect.poll(() => held.length).toBe(2);
    const body = fixture(); body.state = "partial"; body.quality = "partial";
    body.cpu_cores_samples[0].quality = "partial";
    body.memory_bytes_samples[0].quality = "partial";
    await fulfillJson(held[1], body);
    await expect(page.getByTestId("console-workspace-telemetry")).toBeVisible();
    await expect(tabs).toHaveCount(gate === "telemetry" ? 1 : 0);
    for (const id of ["telemetry-mode-label", "telemetry-sample-time", "telemetry-partial-indicator"]) {
      await expect(page.getByTestId(id)).toHaveCount(gate === "telemetry" ? 1 : 0);
    }
    const panel = page.getByTestId("console-workspace-telemetry").locator("xpath=ancestor::section[1]");
    await expect(panel.getByTitle("Showing the last snapshot — live data may be stale"))
      .toHaveCount(gate === "telemetry" ? 1 : 0);
    if (gate === "allocation") {
      await expect(page.getByTestId("telemetry-meter-cpu")).not.toContainText("partial");
      await expect(page.getByTestId("telemetry-meter-memory")).not.toContainText("partial");
    }
    expect(held.map(route => new URL(route.request().url()).searchParams.get("view"))).toEqual(["1h", "1h"]);
    await expect(page.getByTestId("telemetry-series-cpu")).toHaveCount(gate === "telemetry" ? 1 : 0);
    await expect(page.getByTestId("telemetry-workload-cost")).toHaveCount(gate === "cost" ? 1 : 0);
    await expect(page.getByText("Compute class", { exact: true })).toHaveCount(gate === "allocation" ? 1 : 0);
  });
}

// Hold actual dashboard fetches so cancellation and late delivery are exercised
// independently of the reusable component harness.
for (const transition of ["withdrawal", "pending", "malformed", "missing", "outage", "backend", "401", "403"] as const) {
  test(`in-flight telemetry across capability ${transition} and recovery`, async ({ page }) => {
    const caps = await setup(page);
    const held: import("@playwright/test").Route[] = [];
    const cancelled: string[] = [];
    let reads = 0;
    page.on("requestfailed", request => {
      if (request.url().includes("/telemetry?")) cancelled.push(request.url());
    });
    await page.route("**/telemetry?*", async route => {
      reads++;
      if (reads === 1) await fulfillJson(route, fixture());
      else held.push(route);
    });
    await page.clock.install();
    await page.goto("/"); await open(page);
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
    await page.clock.runFor(61_000);
    await expect.poll(() => held.length).toBe(1);
    let capabilityRead: import("@playwright/test").Route | undefined;
    await page.route("**/console/capabilities", async route => { capabilityRead = route; });
    await page.getByRole("button", { name: "Reload workspace" }).click();
    await expect.poll(() => capabilityRead !== undefined).toBe(true);
    if (transition === "pending") {
      // Pending same-context refresh does not withdraw the last negotiation.
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
      expect(cancelled).toHaveLength(0);
    } else {
      const next = structuredClone(caps) as Record<string, unknown>;
      if (transition === "withdrawal") next.widgets = (localCapabilities() as { widgets: unknown }).widgets;
      if (transition === "malformed") next.widgets = [];
      if (transition === "backend") next.identity = { ...(next.identity as object), backend_id: "replacement-backend" };
      const status = transition === "missing" ? 404 : transition === "outage" ? 503 : Number(transition) || 200;
      await fulfillJson(capabilityRead!, status === 200 ? next : { detail: { message: `capability ${transition}` } }, status);
    }
    const retainsNegotiation = transition === "pending" || transition === "outage";
    if (retainsNegotiation) {
      if (transition === "outage") await expect(page.getByText("capability outage", { exact: true })).toBeVisible();
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
      expect(cancelled).toHaveLength(0);
      const fresh = fixture(); fresh.estimate = null;
      await fulfillJson(held[0], fresh);
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Not recorded");
      if (transition === "pending") await fulfillJson(capabilityRead!, caps);
    } else {
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
      await expect.poll(() => cancelled.length).toBe(1);
      await fulfillJson(held[0], fixture());
      await page.clock.runFor(1_000);
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
    }
    const recovered = structuredClone(caps) as Record<string, unknown>;
    if (transition === "backend") recovered.identity = { ...(recovered.identity as object), backend_id: "replacement-backend" };
    let recoveries = 0;
    await page.route("**/console/capabilities", async route => { recoveries++; await fulfillJson(route, recovered); });
    // Keep the inspector mounted; its overlay can cover the header button.
    await page.locator("header").getByRole("button", { name: "Refresh", exact: true })
      .evaluate((button: HTMLButtonElement) => button.click());
    await expect.poll(() => recoveries).toBe(1);
    if (!retainsNegotiation) {
      if (transition === "backend" || transition === "401" || transition === "403") await open(page);
      await expect(page.getByTestId("telemetry-loading")).toBeVisible();
      await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveCount(0);
      await expect.poll(() => held.length).toBe(2);
      const fresh = fixture(); fresh.estimate = null;
      await fulfillJson(held[1], fresh);
    }
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Not recorded");
    expect(reads).toBe(retainsNegotiation ? 2 : 3);
    await page.clock.runFor(59_000);
    expect(reads).toBe(retainsNegotiation ? 2 : 3);
  });
}

test("parent refreshes and unrelated capability revisions preserve telemetry cadence", async ({ page }) => {
  const caps = await setup(page) as Record<string, unknown> & { widgets: Array<Record<string, unknown>> };
  let reads = 0;
  let negotiations = 0;
  await page.route("**/console/capabilities", async route => { negotiations++; await fulfillJson(route, caps); });
  await page.route("**/telemetry?*", async route => { reads++; await fulfillJson(route, fixture()); });
  const start = new Date("2026-09-12T12:01:00Z");
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Open workspace details for ws_unpriced_allocation", exact: true })).toBeVisible();
  await page.clock.install({ time: start });
  // Pause after dashboard initialization, before mounting the telemetry reader.
  // Installation alone still advances with wall time during UI interactions.
  // Use a future timestamp so time spent installing cannot put it in the past.
  await page.clock.pauseAt(new Date(start.getTime() + 60_000));
  await open(page);
  await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
  for (let revision = 0; revision < 4; revision++) {
    await page.clock.runFor(10_000);
    // Identical refresh, timestamp-only refresh, then unrelated inventory edits.
    if (revision === 1) caps.generated_at = "2026-09-12T12:01:00Z";
    if (revision >= 2) caps.widgets = caps.widgets.map(widget => widget.id === "cloud_runtime"
      ? { ...widget, semantics: `Unrelated revision ${revision}` } : widget);
    const before = negotiations;
    await page.getByRole("button", { name: "Reload workspace" }).click();
    await expect.poll(() => negotiations).toBe(before + 1);
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unpriced");
    expect(reads).toBe(1);
  }
  // Forty seconds elapsed across revisions; the original deadline must survive.
  await page.clock.runFor(19_999); expect(reads).toBe(1);
  await page.clock.runFor(1); await expect.poll(() => reads).toBe(2);
});

test("delayed telemetry and dense 24h preserve scroll, selection and bounded history", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const rows = Array.from({ length: 301 }, (_, index) => overview(`ws_interaction_${index}`));
  await mockAwfConsoleApi(page, { capabilities: enabled(), overviewItems: rows.slice(0, 100) });
  const cursors: Array<string | null> = [];
  const batches: string[][] = [];
  await page.route("**/api/awf/workspaces/overview?*", async route => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    cursors.push(cursor);
    const offset = Number(cursor ?? 0);
    await fulfillJson(route, { items: rows.slice(offset, offset + 100), has_more: offset + 100 < rows.length, next_cursor: String(offset + 100) });
  });
  await page.route("**/api/awf/workspaces/overview/batch", async route => {
    const ids = route.request().postDataJSON().workspace_ids as string[];
    batches.push(ids);
    await fulfillJson(route, { items: rows.filter(row => ids.includes(row.workspace_id)), missing_workspace_ids: [] });
  });
  await page.route(/\/api\/awf\/workspaces\/ws_interaction_\d+$/, route => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
    return fulfillJson(route, overview(id));
  });
  const held: import("@playwright/test").Route[] = [];
  await page.route("**/telemetry?*", async route => { held.push(route); });
  await page.goto("/");
  await expect(page.getByTestId("workspace-card-ws_interaction_75")).toBeAttached();
  expect(cursors).toEqual([null]);
  const list = page.getByTestId("workspace-list-scroll");
  const selected = page.getByTestId("workspace-card-ws_interaction_75");
  await selected.evaluate(element => {
    const list = element.closest<HTMLElement>('[data-testid="workspace-list-scroll"]')!;
    const controlsHeight = list.querySelector<HTMLElement>(":scope > .sticky")?.offsetHeight ?? 0;
    list.scrollTo({ top: list.scrollTop + element.getBoundingClientRect().top - list.getBoundingClientRect().top - controlsHeight });
  });
  await page.getByLabel("Select ws_interaction_75 for fullscreen logs").check();
  const scrollBefore = await list.evaluate(element => element.scrollTop);
  expect(scrollBefore).toBeGreaterThan(0);
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  const started = Date.now();
  await open(page, "ws_interaction_75");
  await expect(page.getByTestId("telemetry-loading")).toBeVisible();
  await page.getByRole("button", { name: "Close inspector" }).click();
  await expect(inspector).toHaveClass(/translate-x-full/);
  const delayedPaneMs = Date.now() - started;
  expect(delayedPaneMs).toBeLessThan(1_000);
  await expect.poll(() => list.evaluate(element => element.scrollTop)).toBe(scrollBefore);
  await expect(page.getByLabel("Select ws_interaction_75 for fullscreen logs")).toBeChecked();
  await open(page, "ws_interaction_75");
  await page.getByTestId("telemetry-view-24h").click();
  await expect.poll(() => held.filter(route => new URL(route.request().url()).searchParams.get("view") === "24h").length).toBe(1);
  // A user scroll during the delayed request must survive projection/rendering.
  await list.evaluate(element => element.scrollTo({ top: element.scrollTop + 120 }));
  const duringRead = await list.evaluate(element => element.scrollTop);
  expect(duringRead).toBeGreaterThan(scrollBefore);
  const dense = fixture("day_two_containers");
  expect(dense.cpu_cores_samples).toHaveLength(2048);
  expect(dense.memory_bytes_samples).toHaveLength(2048);
  // Only routing ownership changes in this consumer interaction scenario.
  dense.ownership.workspace_record_id = "ws_interaction_75";
  const dayRead = held.find(route => new URL(route.request().url()).searchParams.get("view") === "24h")!;
  await fulfillJson(dayRead, dense);
  await expect(page.getByTestId("telemetry-series-cpu").locator("svg")).toBeVisible();
  await expect(page.getByTestId("telemetry-view-selector")).toHaveAttribute("data-awf-displayed-view", "24h");
  await expect(page).toHaveURL(/workspaceId=ws_interaction_75/);
  await expect(page.getByLabel("Select ws_interaction_75 for fullscreen logs")).toBeChecked();
  await expect.poll(() => list.evaluate(element => element.scrollTop)).toBe(duringRead);
  expect(cursors.filter(cursor => cursor !== null)).toEqual([]);
  const denseStarted = Date.now();
  await page.getByRole("button", { name: "Close inspector" }).click();
  await expect(inspector).toHaveClass(/translate-x-full/);
  await open(page, "ws_interaction_75");
  await expect(page.getByTestId("telemetry-loading")).toBeVisible();
  const densePaneMs = Date.now() - denseStarted;
  expect(densePaneMs).toBeLessThan(1_000);
  console.info(`[telemetry-interaction] delayed-open-close=${delayedPaneMs}ms dense-close-open=${densePaneMs}ms`);
  await page.getByRole("button", { name: "Close inspector" }).click();
  await list.evaluate(element => element.scrollTo({ top: element.scrollHeight }));
  await expect.poll(() => cursors.filter(cursor => cursor !== null)).toEqual(["100"]);
  // Near-bottom scrolling may advance the virtual window as the page appends.
  await expect(page.getByText(/^(1–100|101–200) of 200 loaded$/)).toBeVisible();
  // Cover a routine parent poll after dense rendering without eager history.
  await page.waitForTimeout(5_500);
  expect(cursors.filter(cursor => cursor !== null)).toEqual(["100"]);
  expect(batches.every(ids => ids.length <= 100)).toBe(true);
  expect(held).toHaveLength(4);
  expect(held.every(route => new URL(route.request().url()).pathname.endsWith("/ws_interaction_75/telemetry"))).toBe(true);
  await expect(page.locator('[data-testid^="workspace-card-"]')).toHaveCount(100);
});
