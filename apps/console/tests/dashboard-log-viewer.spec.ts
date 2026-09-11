import { expect, type Page, test } from "@playwright/test";

import type { AwfStreamFrame } from "@/lib/types";

import { fulfillJson, localCapabilities, localDashboardSummary } from "./fixtures/console-api";
import { streamTest } from "./fixtures/open-event-stream";

const now = "2026-05-21T10:00:00.000Z";
const quietOpenedAt = "2026-05-21T10:00:20.000Z";
const activeOpenedAt = "2026-05-21T10:00:10.000Z";
const quietStreamId = "quiet.stdout";

type MockAwfApiOptions = {
  advanceActiveTailAfterFirstRead?: boolean;
  quietTailBytes?: number;
  streamNoiseBytes?: number;
  streamResponseDelayMs?: number;
};

// Promise executors do not count as assignments for control-flow narrowing, so a
// null-initialized release callback is inferred as never at the call site.
function createDeferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

test("fullscreen logs default to ascending, preserve manual scroll, and order tails by stream activity", async ({
  page,
}) => {
  const api = await mockAwfApi(page);
  api.activeMetadataAdvanced = false;

  await page.goto("/");
  await waitForConsoleReady(page);

  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(modal.getByRole("button", { name: "asc" })).toBeVisible();

  const output = modal.getByTestId("log-output");
  await expect(output).toBeVisible();
  await expect(output).toContainText("active.stdout");
  await expect(output).toContainText("quiet.stdout");
  // The inspector also lists streams. Advance only after fullscreen has its
  // own baseline, not after an arbitrary shared request count.
  api.activeMetadataAdvanced = true;

  await expect
    .poll(
      async () => {
        const text = await output.textContent();
        const value = text ?? "";
        return value.lastIndexOf("active.stdout") > value.lastIndexOf("quiet.stdout");
      },
      { timeout: 15_000 },
    )
    .toBe(true);

  await output.evaluate((node) => {
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  const streamPollsBefore = api.streamPolls;

  await expect.poll(() => api.streamPolls, { timeout: 8_000 }).toBeGreaterThan(streamPollsBefore);
  await expect.poll(async () => output.evaluate((node) => node.scrollTop), { timeout: 15_000 }).toBeLessThan(24);
});

test("fullscreen logs refresh selected tails when stream metadata advances", async ({ page }) => {
  const api = await mockAwfApi(page, { advanceActiveTailAfterFirstRead: true });
  await page.goto("/");
  await waitForConsoleReady(page);

  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText("active line 119 poll 1");

  const streamPollsBefore = api.streamPolls;
  await expect.poll(() => api.streamPolls, { timeout: 8_000 }).toBeGreaterThan(streamPollsBefore);
  await expect.poll(async () => output.textContent() ?? "", { timeout: 8_000 }).toContain("active line 139 poll 2");
});

// Release-audit regression for PR #958: in polling-only mode, each advancing
// metadata poll can start a newer tail before the prior successful tail lands.
// A newer request start is not a newer applied result; the completed last-good
// tail must paint while that newer request is still pending.
test("fullscreen applies a slow successful tail while advancing metadata starts a newer reload", async ({
  page,
}) => {
  test.setTimeout(45_000);
  await mockAwfApi(page);

  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const pollingOnlyCapabilities = {
    ...caps,
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "workspace_stream"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "workspace_stream unavailable",
            semantics: "Optional workspace live event/log stream.",
          }
        : item,
    ),
  };
  let holdSlowTails = false;
  let metadataRevision = 0;
  let slowTailStarts = 0;
  let streamRequests = 0;
  const heldTails: Array<{ promise: Promise<void>; resolve: () => void }> = [];
  const baselineMarker = "baseline-before-slow-polling-tail";
  const firstSlowMarker = "first-slow-success-must-apply";
  const secondSlowMarker = "second-slow-success";

  await page.route("**/api/awf/console/capabilities", async (route) => {
    await fulfillJson(route, pollingOnlyCapabilities);
  });
  await page.route(/\/api\/awf\/workspaces\/ws_logs\/logs(?:\?.*)?$/, async (route) => {
    await fulfillJson(
      route,
      listEnvelope([
        logStream(
          "active.stdout",
          2_880 + metadataRevision,
          120 + metadataRevision,
          activeOpenedAt,
        ),
      ]),
    );
  });
  await page.route(/\/api\/awf\/workspaces\/ws_logs\/logs\/active\.stdout(?:\?.*)?$/, async (route) => {
    if (!holdSlowTails) {
      await fulfillJson(route, logRead("active.stdout", baselineMarker));
      return;
    }
    const gate = createDeferred();
    heldTails.push(gate);
    slowTailStarts += 1;
    const tailNumber = slowTailStarts;
    await gate.promise;
    const marker = tailNumber === 1 ? firstSlowMarker : secondSlowMarker;
    await fulfillJson(route, logRead("active.stdout", marker));
  });
  await page.route(/\/api\/awf\/workspaces\/ws_logs\/stream(?:\?.*)?$/, async (route) => {
    streamRequests += 1;
    await fulfillJson(route, { detail: { message: "workspace_stream unsupported" } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const output = page.locator(".fixed.inset-0.z-50").getByTestId("log-output");
  await expect(output).toContainText(baselineMarker);
  holdSlowTails = true;
  await page.locator(".fixed.inset-0.z-50").getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => slowTailStarts).toBe(1);

  // The dashboard inspector and fullscreen column both read this workspace.
  // Advance their shared metadata only after Tail all has started the first
  // fullscreen read, then wait for both consumers to start their next reads.
  metadataRevision = 1;
  await expect.poll(() => slowTailStarts, { timeout: 15_000 }).toBeGreaterThanOrEqual(3);

  heldTails[0]?.resolve();
  await expect(output).toContainText(firstSlowMarker, { timeout: 4_000 });
  expect(streamRequests).toBe(0);

  for (const heldTail of heldTails.slice(1)) {
    heldTail.resolve();
  }
  await expect(output).toContainText(secondSlowMarker, { timeout: 4_000 });
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hb-HG: a newer
// multi-stream reload can succeed for one stream while another fails. Its
// global generation must not suppress an older success for the failed stream.
test("fullscreen logs apply an older per-stream success after a newer sibling-only success", async ({
  page,
}) => {
  test.setTimeout(45_000);
  await mockAwfApi(page);

  const olderQuiet = createDeferred();
  const hangingQuietRetries: Array<() => void> = [];
  let overlapPhase = false;
  let quietReads = 0;
  let activeReads = 0;
  const baselineQuiet = "baseline-quiet-before-overlap";
  const baselineActive = "baseline-active-before-overlap";
  const olderQuietSuccess = "older-quiet-success-after-newer-failure";
  const olderActiveSuccess = "older-active-success-must-not-rewind";
  const newerActiveSuccess = "newer-active-success-must-remain";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/workspaces/ws_logs/logs") {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/logs/quiet.stdout") {
      if (!overlapPhase) {
        await fulfillJson(route, logRead("quiet.stdout", baselineQuiet));
        return;
      }
      quietReads += 1;
      if (quietReads === 1) {
        await olderQuiet.promise;
        await fulfillJson(route, logRead("quiet.stdout", olderQuietSuccess));
        return;
      }
      if (quietReads === 2) {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "newer quiet tail failed" } },
          503,
        );
        return;
      }
      await new Promise<void>((resolve) => hangingQuietRetries.push(resolve));
      await fulfillJson(route, logRead("quiet.stdout", olderQuietSuccess));
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/logs/active.stdout") {
      if (!overlapPhase) {
        await fulfillJson(route, logRead("active.stdout", baselineActive));
        return;
      }
      activeReads += 1;
      await fulfillJson(
        route,
        logRead("active.stdout", activeReads === 1 ? olderActiveSuccess : newerActiveSuccess),
      );
      return;
    }
    await route.fallback();
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(baselineQuiet);
  await expect(output).toContainText(baselineActive);

  overlapPhase = true;
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => quietReads).toBe(1);
  await expect.poll(() => activeReads).toBe(1);

  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => quietReads).toBe(2);
  await expect.poll(() => activeReads).toBe(2);
  await expect(output).toContainText(newerActiveSuccess);
  await expect(modal.getByRole("alert")).toContainText("newer quiet tail failed");

  olderQuiet.resolve();
  await expect(output).toContainText(olderQuietSuccess);
  await expect(output).toContainText(newerActiveSuccess);
  await expect(output).not.toContainText(olderActiveSuccess);
  await expect(modal.getByRole("alert")).toContainText("newer quiet tail failed");

  for (const resolve of hangingQuietRetries) {
    resolve();
  }
});

test("fullscreen logs keep selected stream history when unselected stream tails are oversized", async ({ page }) => {
  const modalSelector = ".fixed.inset-0.z-50";
  const streamName = "quiet.stdout";
  const activeExpectedText = "active.stdout";

  await mockAwfApi(page, {
    streamNoiseBytes: 220_000,
    streamResponseDelayMs: 150,
  });
  await page.goto("/");
  await waitForConsoleReady(page);

  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(modalSelector);
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();
  const output = modal.getByTestId("log-output");

  await modal.getByRole("checkbox", { name: streamName }).uncheck();

  await expect.poll(async () => output.textContent() ?? "").toContain(activeExpectedText);
});

test("fullscreen logs reload tails after clearing and reselecting the same streams", async ({ page }) => {
  const api = await mockAwfApi(page);
  await page.goto("/");
  await waitForConsoleReady(page);

  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText("active.stdout");

  await modal.getByRole("button", { name: "Clear" }).click();
  await expect(output).toContainText("No log data loaded.");

  api.activeTailPoll = 99;
  await modal.getByRole("button", { name: "All", exact: true }).click();

  await expect(output).toContainText("active line 000 poll 99");
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f-_vJ: a fullscreen
// /logs poll that returns 401/403 while workspace_logs stays advertised must
// drop previously authorized column caches and close the live EventSource.
// An older overlapping listing 200 must not restore them.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs clear caches and ignore overlapping listing success after authorization denial (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let listingMode: "ok" | "delay_ok" | "denied" = "ok";
  let delayedListingStarts = 0;
  let delayedListingFinished = 0;
  const workspaceId = "ws_fs_log_auth";
  const authorizedMarker = "authorized-fullscreen-log-line";
  const revokedMarker = "stale-listing-success-must-not-restore";
  const liveSecret = "live-stream-after-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      if (listingMode === "delay_ok") {
        delayedListingStarts += 1;
        // Longer than pollMs so the next poll's 403 overlaps this success.
        await new Promise((resolve) => setTimeout(resolve, 8_000));
        await fulfillJson(
          route,
          listEnvelope([logStream(revokedMarker, 64, 1, now)]),
        );
        delayedListingFinished += 1;
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(revokedMarker)}`) {
      await fulfillJson(route, logRead(revokedMarker, revokedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      const frames: AwfStreamFrame[] = [{ type: "connected", workspace_id: workspaceId }];
      if (listingMode === "denied") {
        frames.push({
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        });
      }
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  listingMode = "delay_ok";
  await expect.poll(() => delayedListingStarts, { timeout: 12_000 }).toBeGreaterThan(0);
  listingMode = "denied";

  await expect(modal.getByText("log listing permission revoked")).toBeVisible({ timeout: 12_000 });
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText("No log streams recorded.")).toBeVisible();
  await expect(modal.getByText(authorizedMarker)).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);

  await expect.poll(() => delayedListingFinished, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(revokedMarker)).toHaveCount(0);
  await expect(modal.getByText(authorizedMarker)).toHaveCount(0);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText("log listing permission revoked")).toBeVisible();
  await expect(modal.getByText(/stream idle/)).toBeVisible();

  // Inspector live-stream shares this path, so request count is not a close
  // signal. The column must stay idle and must not replay denied frames.
  await page.waitForTimeout(4_000);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(modal.getByText(revokedMarker)).toHaveCount(0);
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCA3J: when two
// fullscreen /logs listing polls overlap and the newer 200 applies first, the
// older 200 must not overwrite streams with its stale snapshot.
test("fullscreen logs ignore an older overlapping listing success after a newer 200", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let listingMode: "ok" | "hold_older" | "newer" = "ok";
  let olderStarted = 0;
  let olderFinished = 0;
  const olderHeld = createDeferred();
  const workspaceId = "ws_fs_log_stale_success";
  const baselineStream = "baseline.stdout";
  const discoveredStream = "discovered.stdout";
  const staleStream = "stale.stdout";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "hold_older" && olderStarted === 0) {
        olderStarted += 1;
        await olderHeld.promise;
        await fulfillJson(route, listEnvelope([logStream(staleStream, 64, 1, now)]));
        olderFinished += 1;
        return;
      }
      if (listingMode === "newer" || listingMode === "hold_older") {
        await fulfillJson(
          route,
          listEnvelope([
            logStream(baselineStream, 2_400, 10, quietOpenedAt),
            logStream(discoveredStream, 2_880, 12, activeOpenedAt),
          ]),
        );
        return;
      }
      await fulfillJson(route, listEnvelope([logStream(baselineStream, 2_400, 10, quietOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(baselineStream)}`) {
      await fulfillJson(route, logRead(baselineStream, "baseline-fullscreen-log-line"));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(discoveredStream)}`) {
      await fulfillJson(route, logRead(discoveredStream, "discovered-fullscreen-log-line"));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(staleStream)}`) {
      await fulfillJson(route, logRead(staleStream, "stale-listing-must-not-restore"));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByRole("checkbox", { name: baselineStream })).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: discoveredStream })).toHaveCount(0);

  listingMode = "hold_older";
  await expect.poll(() => olderStarted, { timeout: 12_000 }).toBe(1);
  listingMode = "newer";

  await expect(modal.getByRole("checkbox", { name: discoveredStream })).toBeVisible({ timeout: 15_000 });
  await expect(modal.getByRole("checkbox", { name: baselineStream })).toBeVisible();

  olderHeld.resolve();
  await expect.poll(() => olderFinished, { timeout: 12_000 }).toBe(1);
  // The stale 200 has been delivered. Give React a chance to apply it so a
  // missing generation guard fails instead of racing the assertion.
  await page.waitForTimeout(1_000);
  await expect(modal.getByRole("checkbox", { name: discoveredStream })).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: baselineStream })).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: staleStream })).toHaveCount(0);
  await expect(modal.getByText("stale-listing-must-not-restore")).toHaveCount(0);
});

// Regression: start poll1; before it returns 401/403 start poll2; resolve
// poll1; repeat. Discarding denials with generation !== listingGenerationRef
// leaves cached private tails and EventSource open when every denial is
// slower than pollMs. A later listing 200 must still be able to recover.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs apply slow listing denial while a newer poll is in flight (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(90_000);
  let listingMode: "ok" | "slow_denied" | "settle_denied" | "recover" = "ok";
  let slowDeniedStarted = 0;
  let slowDeniedFinished = 0;
  const heldDenied: Array<() => void> = [];
  const workspaceId = "ws_fs_log_slow_denial";
  const authorizedMarker = "authorized-private-log-tail";
  const recoveryMarker = "listing-recovered-after-denial";
  const liveSecret = "live-stream-during-slow-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "slow_denied") {
        slowDeniedStarted += 1;
        // Hold past the next pollMs tick so a newer listing poll starts first.
        await new Promise<void>((resolve) => {
          heldDenied.push(resolve);
        });
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        slowDeniedFinished += 1;
        return;
      }
      if (listingMode === "settle_denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      if (listingMode === "recover") {
        await fulfillJson(route, listEnvelope([logStream("recovered.stdout", 64, 1, now)]));
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/recovered.stdout`) {
      await fulfillJson(route, logRead("recovered.stdout", recoveryMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      const frames: AwfStreamFrame[] = [{ type: "connected", workspace_id: workspaceId }];
      if (listingMode === "slow_denied") {
        frames.push({
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        });
      }
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  // Playwright's fulfilled EventSource often stays "connecting"; denial must
  // still force idle and must not leave that live/connecting column open.
  await expect(modal.getByText(/stream (connecting|live)/)).toBeVisible();

  listingMode = "slow_denied";

  for (let round = 0; round < 4; round += 1) {
    // A newer poll must already be in flight before this denial resolves.
    await expect.poll(() => slowDeniedStarted, { timeout: 20_000 }).toBeGreaterThanOrEqual(round + 2);
    const release = heldDenied.shift();
    expect(release, `held listing denial ${round}`).toBeTruthy();
    release?.();
    await expect.poll(() => slowDeniedFinished, { timeout: 10_000 }).toBe(round + 1);
    expect(slowDeniedStarted).toBeGreaterThan(slowDeniedFinished);

    await expect(modal.getByText("log listing permission revoked")).toBeVisible();
    await expect(output).toContainText("No log data loaded.");
    await expect(modal.getByText("No log streams recorded.")).toBeVisible();
    await expect(modal.getByText(authorizedMarker)).toHaveCount(0);
    await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
    await expect(modal.getByText(/stream idle/)).toBeVisible();
    await expect(modal.getByText(liveSecret)).toHaveCount(0);
  }

  // Stop holding so in-flight denials can settle before a recovery poll starts.
  // A 403 released after recovery has begun must not be the only path that
  // proves a later 200 can apply.
  listingMode = "settle_denied";
  while (heldDenied.length > 0) {
    heldDenied.shift()?.();
  }
  await expect.poll(() => slowDeniedStarted === slowDeniedFinished && heldDenied.length === 0, {
    timeout: 10_000,
  }).toBe(true);
  listingMode = "recover";
  await expect(output).toContainText(recoveryMarker, { timeout: 15_000 });
  await expect(modal.getByText(authorizedMarker)).toHaveCount(0);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(modal.getByText("log listing permission revoked")).toHaveCount(0);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCS0A: when /logs
// consistently takes longer than pollMs and returns network/5xx, the interval
// starts a newer generation before each prior request settles. Discarding
// those failures because generation !== listingGenerationRef.current leaves
// the column blank or retains last-good streams with no error. Suppress a
// failure only after a newer listing 200 has applied.
test("fullscreen logs apply slow listing failures while a newer poll is in flight", async ({
  page,
}) => {
  test.setTimeout(90_000);
  let listingMode: "slow_fail" | "ok" = "slow_fail";
  let slowFailStarted = 0;
  let slowFailFinished = 0;
  const heldFailures: Array<() => void> = [];
  const workspaceId = "ws_fs_log_slow_fail";
  const outageMessage = "log listing feed outage";
  const retainedStream = "retained.stdout";
  const retainedMarker = "retained-listing-after-outage";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "slow_fail") {
        slowFailStarted += 1;
        // Hold past the next pollMs tick so a newer listing poll starts first.
        await new Promise<void>((resolve) => {
          heldFailures.push(resolve);
        });
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          503,
        );
        slowFailFinished += 1;
        return;
      }
      await fulfillJson(route, listEnvelope([logStream(retainedStream, 64, 1, now)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(retainedStream)}`) {
      await fulfillJson(route, logRead(retainedStream, retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();

  for (let round = 0; round < 2; round += 1) {
    // A newer poll must already be in flight before this failure resolves.
    await expect.poll(() => slowFailStarted, { timeout: 20_000 }).toBeGreaterThanOrEqual(round + 2);
    const release = heldFailures.shift();
    expect(release, `held listing failure ${round}`).toBeTruthy();
    release?.();
    await expect.poll(() => slowFailFinished, { timeout: 10_000 }).toBe(round + 1);
    expect(slowFailStarted).toBeGreaterThan(slowFailFinished);

    await expect(modal.getByText(outageMessage)).toBeVisible();
    await expect(output).toContainText("No log data loaded.");
    await expect(modal.getByText("No log streams recorded.")).toBeVisible();
    await expect(modal.getByRole("checkbox", { name: retainedStream })).toHaveCount(0);
  }

  listingMode = "ok";
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible({ timeout: 15_000 });
  await expect(output).toContainText(retainedMarker, { timeout: 12_000 });
  await expect(modal.getByText(outageMessage)).toHaveCount(0);

  // Older failures that started before this 200 must not restore the warning.
  while (heldFailures.length > 0) {
    heldFailures.shift()?.();
  }
  await expect.poll(() => slowFailStarted === slowFailFinished && heldFailures.length === 0, {
    timeout: 10_000,
  }).toBe(true);
  await page.waitForTimeout(1_000);
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible();
  await expect(output).toContainText(retainedMarker);

  listingMode = "slow_fail";
  const startedBeforeOutage = slowFailStarted;
  await expect.poll(() => slowFailStarted, { timeout: 20_000 }).toBeGreaterThanOrEqual(startedBeforeOutage + 2);
  const overlappingFailure = heldFailures.shift();
  expect(overlappingFailure, "held listing failure after recovery").toBeTruthy();
  overlappingFailure?.();
  await expect.poll(() => slowFailFinished, { timeout: 10_000 }).toBeGreaterThan(startedBeforeOutage);
  expect(slowFailStarted).toBeGreaterThan(slowFailFinished);

  await expect(modal.getByText(outageMessage)).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible();
  await expect(output).toContainText(retainedMarker);
});

// A newer listing 5xx can apply while an older 200 is still in flight.
// That older success must not clear the outage warning or rewind streams.
test("fullscreen logs keep a newer listing failure after an older success settles", async ({
  page,
}) => {
  test.setTimeout(90_000);
  type ListingOutcome = "fail" | "ok";
  let listingMode: "ok" | "overlap" = "ok";
  let listingStarted = 0;
  let listingFinished = 0;
  const heldListings: Array<{ seq: number; finish: (outcome: ListingOutcome) => void }> = [];
  const workspaceId = "ws_fs_log_fail_vs_old_ok";
  const outageMessage = "log listing feed outage after snapshot";
  const retainedStream = "retained.stdout";
  const retainedMarker = "retained-listing-before-newer-outage";
  const staleMarker = "stale-listing-must-not-replace";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "overlap") {
        listingStarted += 1;
        const seq = listingStarted;
        const outcome = await new Promise<ListingOutcome>((resolve) => {
          heldListings.push({ seq, finish: resolve });
        });
        if (outcome === "fail") {
          await fulfillJson(
            route,
            { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
            503,
          );
        } else {
          await fulfillJson(route, listEnvelope([logStream("stale.stdout", 32, 1, now)]));
        }
        listingFinished += 1;
        return;
      }
      await fulfillJson(route, listEnvelope([logStream(retainedStream, 64, 1, now)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent(retainedStream)}`) {
      await fulfillJson(route, logRead(retainedStream, retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${encodeURIComponent("stale.stdout")}`) {
      await fulfillJson(route, logRead("stale.stdout", staleMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible({ timeout: 15_000 });
  await expect(output).toContainText(retainedMarker, { timeout: 12_000 });

  listingMode = "overlap";
  // Inspector detail and the fullscreen column both poll /logs. Hold until each
  // has an older request plus a newer one, then fail every newer request so the
  // modal definitely applies a 503 before its older 200 is released.
  await expect.poll(() => heldListings.length, { timeout: 20_000 }).toBeGreaterThanOrEqual(4);
  const snapshot = heldListings.splice(0, heldListings.length);
  const older = snapshot.slice(0, 2);
  const newer = snapshot.slice(2);
  expect(newer.length).toBeGreaterThan(0);
  expect(newer[0]?.seq).toBeGreaterThan(older[0]?.seq ?? 0);
  for (const item of newer) {
    item.finish("fail");
  }
  await expect(modal.getByText(outageMessage)).toBeVisible({ timeout: 15_000 });
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible();
  await expect(output).toContainText(retainedMarker);

  for (const item of older) {
    item.finish("ok");
  }
  await expect.poll(() => listingFinished, { timeout: 10_000 }).toBeGreaterThanOrEqual(snapshot.length);
  await expect(modal.getByText(outageMessage)).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: "stale.stdout" })).toHaveCount(0);
  await expect(output).toContainText(retainedMarker);
  await expect(output).not.toContainText(staleMarker);

  listingMode = "ok";
  while (heldListings.length > 0) {
    heldListings.shift()?.finish("fail");
  }
  await expect.poll(() => listingStarted === listingFinished && heldListings.length === 0, {
    timeout: 10_000,
  }).toBe(true);
  await expect(modal.getByText(outageMessage)).toHaveCount(0, { timeout: 15_000 });
  await expect(modal.getByRole("checkbox", { name: retainedStream })).toBeVisible();
  await expect(output).toContainText(retainedMarker);
});

test("fullscreen logs trap keyboard focus and restore it to the trigger on close", async ({ page }) => {
  await mockAwfApi(page);
  await page.goto("/");
  await waitForConsoleReady(page);

  const trigger = page
    .getByTestId("workspace-card-ws_logs")
    .getByRole("button", { name: "Logs", exact: true });
  await trigger.click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();

  // Focus moves into the dialog on open.
  await expect.poll(() => page.evaluate(() => document.activeElement?.getAttribute("role"))).toBe("dialog");

  // Tabbing from the last focusable element cycles back inside the dialog,
  // never escaping to the page behind the overlay.
  for (let i = 0; i < 30; i += 1) {
    await page.keyboard.press("Tab");
    const insideDialog = await page.evaluate(() => {
      const dialog = document.querySelector('[role="dialog"]');
      return dialog ? dialog.contains(document.activeElement) : false;
    });
    expect(insideDialog).toBe(true);
  }

  // Escape closes the dialog and restores focus to the element that opened it.
  await page.keyboard.press("Escape");
  await expect(modal).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6f_oIB: the inspector
// detail loader clears only detail.streams on a /logs 401/403 while
// workspace_logs stays advertised. Retained selection, tail caches, and the
// still-open EventSource must drop previously authorized log text.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs clear selection caches and ignore live frames after listing authorization denial (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let listingMode: "ok" | "denied" = "ok";
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_log_auth";
  const authorizedMarker = "authorized-inspector-log-line";
  const liveSecret = "inspector-live-stream-after-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      // Hold the already-open inspector EventSource until listing denial is
      // applied, then deliver a log frame. Capability negotiation still
      // advertises workspace_logs, so the stream gate would otherwise append it.
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  listingMode = "denied";

  await expect(inspector.getByText("No log streams recorded.")).toBeVisible({ timeout: 12_000 });
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText("No log streams recorded.")).toBeVisible();
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
  // Denial closes the inspector EventSource; the capability gate stays true.
  await expect(page.getByText("Stream: idle")).toBeVisible();

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gD08-: a /logs 401/403
// must clear inspector log caches and close EventSource without waiting for a
// sibling runtime/events/operations request. Promise.all never settled while
// that sibling hung, so previously authorized log text stayed available.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs apply listing denial without waiting for a hanging sibling (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let listingMode: "ok" | "split" = "ok";
  let streamOpens = 0;
  const hangingSibling = createDeferred();
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_log_hanging_sibling";
  const authorizedMarker = "authorized-inspector-log-line";
  const liveSecret = "inspector-live-frame-before-hanging-sibling-must-not-appear";
  const siblingAfterDenial = "hanging-runtime-must-not-restore-inspector-logs";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (listingMode === "split") {
        await hangingSibling.promise;
        await fulfillJson(route, {
          stack_state: "running",
          services: [],
          app_endpoints: [],
          compose_project_name: siblingAfterDenial,
        });
        return;
      }
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "split") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  listingMode = "split";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });

  // Runtime is still pending. Listing denial must already have cleared caches
  // and closed /stream; waiting for Promise.all would leave the prior snapshot.
  await expect(inspector.getByText(/log listing permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(inspector.getByText("No log streams recorded.")).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  hangingSibling.resolve();
  await expect(inspector.getByText(/log listing permission revoked/i)).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(page.getByText("Stream: idle")).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEfkO: a /logs 401/403
// that settles after Refresh has started a newer detail load must still clear
// inspector caches and close EventSource. A newer request merely starting or
// later returning an in-flight 200 is not recovery; only a listing 200 that
// has already applied, or a later request that starts after the denial, is.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs apply superseded listing denial while a newer refresh hangs (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(60_000);
  let listingMode: "ok" | "hold-deny" | "hang" | "recover" = "ok";
  let streamOpens = 0;
  const heldDeny: Array<() => Promise<void>> = [];
  const hanging: Array<() => Promise<void>> = [];
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_log_superseded_listing_denial";
  const authorizedMarker = "authorized-inspector-log-before-superseded-listing-denial";
  const inflightMarker = "in-flight-listing-must-not-restore";
  const recoveryMarker = "listing-recovered-after-superseded-denial";
  const liveSecret = "inspector-live-frame-after-superseded-listing-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (
      path === `/api/awf/workspaces/${workspaceId}/runtime` ||
      path === `/api/awf/workspaces/${workspaceId}/events` ||
      path === `/api/awf/workspaces/${workspaceId}/operations` ||
      path === `/api/awf/workspaces/${workspaceId}`
    ) {
      if (path.endsWith("/events") || path.endsWith("/operations")) {
        await fulfillJson(route, listEnvelope([]));
        return;
      }
      if (path.endsWith("/runtime")) {
        await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
        return;
      }
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "hold-deny") {
        await new Promise<void>((resolve) => {
          heldDeny.push(async () => {
            await fulfillJson(
              route,
              {
                detail: {
                  error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                  message: "log listing permission revoked",
                },
              },
              deniedStatus,
            );
            resolve();
          });
        });
        return;
      }
      if (listingMode === "hang") {
        await new Promise<void>((resolve) => {
          hanging.push(async () => {
            await fulfillJson(route, listEnvelope([logStream("stale-inflight.stdout", 64, 1, now)]));
            resolve();
          });
        });
        return;
      }
      if (listingMode === "recover") {
        await fulfillJson(route, listEnvelope([logStream("recovered.stdout", 64, 1, now)]));
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/stale-inflight.stdout`) {
      await fulfillJson(route, logRead("stale-inflight.stdout", inflightMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/recovered.stdout`) {
      await fulfillJson(route, logRead("recovered.stdout", recoveryMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  listingMode = "hold-deny";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect.poll(() => heldDeny.length, { timeout: 10_000 }).toBe(1);

  // A second matching /logs is not delivered while the first handler waits.
  // Click in-page so loadWorkspace advances generation before we release the
  // older 401/403. Playwright's click can return before that handler runs
  // while a route is held, which would treat the later listing as recovery.
  listingMode = "hang";
  await page.locator("header").getByRole("button", { name: "Refresh" }).evaluate((button: HTMLButtonElement) => {
    button.click();
  });

  await heldDeny[0]();
  await expect(inspector.getByText(/log listing permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(inspector.getByText("No log streams recorded.")).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  await expect.poll(() => hanging.length, { timeout: 10_000 }).toBe(1);
  await hanging[0]();
  await page.waitForTimeout(750);
  await expect(inspector.getByText(/log listing permission revoked/i)).toBeVisible();
  await expect(inspector.getByText(inflightMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "stale-inflight.stdout" })).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(page.getByText("Stream: idle")).toBeVisible();
  expect(streamOpens).toBe(opensAtDenial);

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(1_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  expect(streamOpens).toBe(opensAtDenial);

  listingMode = "recover";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });
  await expect(output).toContainText(recoveryMarker, { timeout: 12_000 });
  await expect(inspector.getByText(/log listing permission revoked/i)).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByText(inflightMarker)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 10_000 }).toBeGreaterThan(opensAtDenial);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gD08-: a /workspaces/{id}
// 401/403 must also drop cached inspector log text without waiting for a
// sibling runtime/events/operations request. Closing EventSource alone leaves
// the previous tail available indefinitely while that sibling hangs.
for (const deniedStatus of [401, 403] as const) {
test(`inspector drops cached logs on base-detail denial without waiting for a hanging sibling (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let detailMode: "ok" | "split" = "ok";
  let streamOpens = 0;
  const hangingSibling = createDeferred();
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_detail_hanging_sibling_logs";
  const authorizedMarker = "authorized-inspector-log-before-detail-denial";
  const liveSecret = "inspector-live-frame-after-detail-denial-must-not-appear";
  const siblingAfterDenial = "hanging-runtime-must-not-restore-detail-logs";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      if (detailMode === "split") {
        await hangingSibling.promise;
        await fulfillJson(route, {
          stack_state: "running",
          services: [],
          app_endpoints: [],
          compose_project_name: siblingAfterDenial,
        });
        return;
      }
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      if (detailMode === "split") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "workspace detail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          offset: 0,
          next_offset: liveSecret.length,
          data: liveSecret,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  detailMode = "split";
  await page.getByRole("button", { name: /refresh/i }).click({ force: true });

  // Runtime is still pending. Workspace denial must already have cleared log
  // caches and closed /stream; waiting for Promise.all would leave the tail.
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(inspector.getByText("No log streams recorded.")).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  hangingSibling.resolve();
  await expect(inspector.getByText(/workspace detail permission revoked/i)).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(page.getByText("Stream: idle")).toBeVisible();
});
}

// Regression for PR #933 review 5135360306: a detail poll installs a new
// streams array even when metadata is unchanged. Restarting the inspector
// tail on that identity change discards a /logs/{stream} read slower than
// the poll cycle, so an unsupported workspace_stream leaves the inspector empty.
test("inspector applies a slow log tail when listing polls replace stream arrays and live stream is unsupported", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let listingPolls = 0;
  let tailStarts = 0;
  const marker = "slow-inspector-tail-must-apply-without-live-stream";
  const workspaceId = "ws_inspector_slow_tail";
  const caps = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const capabilities = {
    ...caps,
    diagnostics: caps.diagnostics.map((item) =>
      item.id === "workspace_stream"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "not_implemented",
            message: "workspace_stream unavailable",
            semantics: "Optional workspace live event/log stream.",
          }
        : item,
    ),
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, capabilities);
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      listingPolls += 1;
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      tailStarts += 1;
      // Longer than pollMs so the next selected-workspace detail poll installs
      // a new streams array before this read settles.
      await new Promise((resolve) => setTimeout(resolve, 6_500));
      await fulfillJson(route, logRead("active.stdout", marker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await fulfillJson(route, { detail: { message: "workspace_stream unsupported" } }, 404);
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  await expect.poll(() => tailStarts, { timeout: 10_000 }).toBeGreaterThan(0);
  const startsBeforeNextListing = tailStarts;
  await expect.poll(() => listingPolls, { timeout: 12_000 }).toBeGreaterThanOrEqual(2);
  expect(tailStarts).toBe(startsBeforeNextListing);

  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(marker, { timeout: 15_000 });
  expect(tailStarts).toBe(startsBeforeNextListing);
  await expect(page.getByText("Stream: idle")).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gARNY: a /logs/{stream}
// 401/403 while listing stays reachable must drop authorized tail contents and
// close the inspector EventSource. Listing success must not reopen /stream or
// let a queued live frame refill the revoked output.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs close live stream after tail authorization denial while listing stays reachable (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailMode: "ok" | "denied" = "ok";
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_tail_auth";
  const authorizedMarker = "authorized-inspector-tail-line";
  const liveSecret = "inspector-live-stream-after-tail-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      // Hold the already-open inspector EventSource until tail denial is
      // applied, then deliver a log frame. Listing stays 200, so the
      // capability gate and listing latch would otherwise keep /stream open.
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  tailMode = "denied";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();

  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect(page.getByText("Stream: idle")).toBeVisible();

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(output).not.toContainText(authorizedMarker);
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(page.getByText("Stream: idle")).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gEfkW: a manual Tail
// click increments the per-stream generation before an older automatic or
// manual read returns 401/403. Discarding that denial because the newer
// request started leaves cached tails and EventSource open if the newer
// request hangs or fails transiently. A later request that starts after the
// denial may still recover.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs apply tail denial while a newer reload is in flight (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "hold" | "recover" = "ok";
  let olderStarted = 0;
  let newerStarted = 0;
  let streamOpens = 0;
  const heldTails: Array<{ promise: Promise<void>; resolve: () => void }> = [];
  const heldStream = createDeferred();
  const workspaceId = "ws_inspector_tail_slow_denial";
  const authorizedMarker = "authorized-inspector-tail-before-slow-denial";
  const recoveryMarker = "inspector-tail-recovered-after-slow-denial";
  const liveSecret = "inspector-live-frame-during-slow-tail-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "hold") {
        const index = heldTails.length;
        const gate = createDeferred();
        heldTails.push(gate);
        if (index === 0) {
          olderStarted = 1;
        } else {
          newerStarted += 1;
        }
        await gate.promise;
        if (index === 0) {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "log tail permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(route, { detail: { message: "transient tail refresh failed" } }, 500);
        return;
      }
      if (tailPhase === "recover") {
        await fulfillJson(route, logRead("active.stdout", recoveryMarker));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);

  tailPhase = "hold";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect.poll(() => olderStarted, { timeout: 12_000 }).toBe(1);
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect.poll(() => newerStarted, { timeout: 12_000 }).toBe(1);

  const older = heldTails[0];
  const newer = heldTails[1];
  expect(older, "held older tail denial").toBeTruthy();
  expect(newer, "held newer tail reload").toBeTruthy();
  older?.resolve();

  // The newer reload is still pending. Denial must already have cleared caches
  // and closed /stream; waiting for that newer request would leave the snapshot.
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  newer?.resolve();
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).not.toContainText(authorizedMarker);
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();

  heldStream.resolve();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(1_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(output).not.toContainText(authorizedMarker);
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();

  tailPhase = "recover";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect(output).toContainText(recoveryMarker, { timeout: 12_000 });
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCZwi: a 401/403 used
// to keep the automatic-tail fingerprint, so later authorized listing polls
// with unchanged metadata started zero retries and the latch stayed closed
// after access returned. Retry is once per listing refresh, not an immediate
// loop, and stays closed until this stream's own 200.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs retry a denied tail on unchanged listing refresh and recover (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(60_000);
  let tailMode: "ok" | "denied" | "recover" = "ok";
  let listingPolls = 0;
  let tailReads = 0;
  let streamOpens = 0;
  const workspaceId = "ws_inspector_tail_auth_refresh";
  const authorizedMarker = "inspector-tail-recovered-after-static-listing";
  const liveSecret = "inspector-live-frame-before-denied-tail-recovers";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      listingPolls += 1;
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      tailReads += 1;
      if (tailMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: streamOpens,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  const readsBeforeDenial = tailReads;
  const opensBeforeDenial = streamOpens;

  tailMode = "denied";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const readsAtDenial = tailReads;
  expect(readsAtDenial).toBeGreaterThan(readsBeforeDenial);
  const listingAtDenial = listingPolls;

  await page.waitForTimeout(400);
  expect(tailReads - readsAtDenial).toBeLessThanOrEqual(1);

  tailMode = "recover";
  await expect.poll(() => tailReads, { timeout: 20_000 }).toBeGreaterThan(readsAtDenial);
  await expect.poll(() => listingPolls, { timeout: 20_000 }).toBeGreaterThan(listingAtDenial);
  await expect(output).toContainText(authorizedMarker, { timeout: 12_000 });
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensBeforeDenial);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBlfk: a 200 from a
// sibling tail must not clear a 401/403 latched for another selected stream,
// including when the denied stream's retry hangs or returns 5xx. EventSource
// stays closed until that denied stream itself succeeds.
for (const deniedStatus of [401, 403] as const) {
test(`inspector logs keep tail denial latched until the denied stream recovers (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "sibling_success" | "denied_retry" | "recover" = "ok";
  const siblingAfterDenial = createDeferred();
  const deniedRetryRelease = createDeferred();
  let streamOpens = 0;
  const workspaceId = "ws_inspector_tail_sibling_auth";
  const quietMarker = "authorized-quiet-inspector-tail";
  const activeMarker = "authorized-active-inspector-tail";
  const liveSecret = "inspector-denied-stream-live-frame-must-not-reappear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "sibling_success") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      if (tailPhase === "denied_retry") {
        await deniedRetryRelease.promise;
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "denied stream retry failed" } },
          503,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "sibling_success") {
        await siblingAfterDenial.promise;
        await fulfillJson(route, logRead("active.stdout", activeMarker));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", activeMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] =
        tailPhase === "ok"
          ? [{ type: "connected", workspace_id: workspaceId }]
          : [
              { type: "connected", workspace_id: workspaceId },
              {
                type: "log",
                seq: 1,
                workspace_id: workspaceId,
                stream_id: "quiet.stdout",
                source: "monitor",
                fd: "stdout",
                data: liveSecret,
                offset: 0,
                next_offset: liveSecret.length,
                occurred_at: now,
              },
            ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);
  await expect(inspector.getByRole("checkbox", { name: "quiet.stdout" })).toBeVisible();
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  tailPhase = "sibling_success";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();

  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  siblingAfterDenial.resolve();
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();

  tailPhase = "denied_retry";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);

  deniedRetryRelease.resolve();
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);

  tailPhase = "recover";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();
  await expect(output).toContainText(quietMarker, { timeout: 12_000 });
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gDCXO: a denied tail
// that the operator deselects—or that leaves the latest listing—must not stay
// in the inspector denial set and keep EventSource closed after a sibling 200.
// A denial that is still selected and listed still blocks.
test("inspector logs recover tail denial after the denied stream is deselected", async ({ page }) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "held" | "remaining" | "late" = "ok";
  let listedStreamIds = ["quiet.stdout", "active.stdout"];
  const siblingAfterDenial = createDeferred();
  const lateDenial = createDeferred();
  let quietLateRequests = 0;
  let streamOpens = 0;
  const workspaceId = "ws_inspector_tail_deselect_auth";
  const quietMarker = "authorized-quiet-inspector-tail";
  const activeMarker = "authorized-active-inspector-tail";
  const recoveredMarker = "remaining-selected-tail-after-denied-stream-deselected";
  const liveSecret = "inspector-denied-stream-live-frame-must-not-reappear-after-deselect";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(
        route,
        listEnvelope(listedStreamIds.map((streamId) =>
          logStream(
            streamId,
            streamId.startsWith("active") ? 2_880 : 2_400,
            120,
            streamId.startsWith("active") ? activeOpenedAt : quietOpenedAt,
          ),
        )),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "late") {
        quietLateRequests += 1;
        await lateDenial.promise;
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      if (tailPhase !== "ok") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "held") {
        await siblingAfterDenial.promise;
        await fulfillJson(route, logRead("active.stdout", recoveredMarker));
        return;
      }
      await fulfillJson(
        route,
        logRead("active.stdout", tailPhase === "remaining" ? recoveredMarker : activeMarker),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] =
        tailPhase === "ok"
          ? [{ type: "connected", workspace_id: workspaceId }]
          : [
              { type: "connected", workspace_id: workspaceId },
              {
                type: "log",
                seq: 1,
                workspace_id: workspaceId,
                stream_id: "quiet.stdout",
                source: "monitor",
                fd: "stdout",
                data: liveSecret,
                offset: 0,
                next_offset: liveSecret.length,
                occurred_at: now,
              },
            ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);
  await expect(inspector.getByRole("checkbox", { name: "quiet.stdout" })).toBeVisible();
  await expect(inspector.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  tailPhase = "held";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();

  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  siblingAfterDenial.resolve();
  await expect(output).toContainText(recoveredMarker, { timeout: 12_000 });
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);

  tailPhase = "remaining";
  await inspector.getByRole("checkbox", { name: "quiet.stdout" }).uncheck();
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect(output).toContainText(recoveredMarker);
  await expect(output).not.toContainText(quietMarker);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);

  // A 401/403 that returns after the stream has left the listing must not
  // re-enter the denial set. Selected∩listed is what the inspector can retry.
  tailPhase = "late";
  await inspector.getByRole("checkbox", { name: "quiet.stdout" }).check();
  await expect.poll(() => quietLateRequests, { timeout: 12_000 }).toBeGreaterThan(0);
  listedStreamIds = ["active.stdout"];
  await expect(inspector.getByRole("checkbox", { name: "quiet.stdout" })).toHaveCount(0, { timeout: 12_000 });
  const opensBeforeLateDenial = streamOpens;
  lateDenial.resolve();
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThanOrEqual(opensBeforeLateDenial);
  await expect(page.getByText("Stream: idle")).toHaveCount(0);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(output).toContainText(recoveredMarker);

  tailPhase = "remaining";
  listedStreamIds = ["quiet.stdout", "active.stdout"];
  await expect(inspector.getByRole("checkbox", { name: "quiet.stdout" })).toBeVisible({ timeout: 12_000 });
  await inspector.getByRole("checkbox", { name: "quiet.stdout" }).check();
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  const opensAfterReselectDenial = streamOpens;

  listedStreamIds = ["active.stdout"];
  await expect(inspector.getByRole("checkbox", { name: "quiet.stdout" })).toHaveCount(0, { timeout: 12_000 });
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAfterReselectDenial);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBuRs: a 200 deletes
// that stream from the denied set, then previously skipped setLogEntries while
// the workspace latch was still held for a sibling. Earlier recovered inspector
// tails must stay visible; EventSource stays closed until every denied stream
// succeeds.
test("inspector logs keep earlier recovered tails while a sibling denial holds the latch", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "deny_both" | "recover_quiet" | "recover_active" = "ok";
  const activeRecoverRelease = createDeferred();
  let streamOpens = 0;
  const workspaceId = "ws_inspector_tail_partial_recover";
  const quietMarker = "authorized-quiet-inspector-partial";
  const activeMarker = "authorized-active-inspector-partial";
  const quietRecovered = "recovered-quiet-inspector-tail";
  const activeRecovered = "recovered-active-inspector-tail";
  const liveSecret = "inspector-partial-recover-live-frame-must-not-reappear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z" });
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, { generated_at: "2026-09-06T17:00:00Z", active: 1, failed: 0 });
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "deny_both") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      if (tailPhase === "recover_quiet" || tailPhase === "recover_active") {
        await fulfillJson(route, logRead("quiet.stdout", quietRecovered));
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "deny_both") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      if (tailPhase === "recover_quiet" || tailPhase === "recover_active") {
        await activeRecoverRelease.promise;
        await fulfillJson(route, logRead("active.stdout", activeRecovered));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", activeMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] =
        tailPhase === "ok"
          ? [{ type: "connected", workspace_id: workspaceId }]
          : [
              { type: "connected", workspace_id: workspaceId },
              {
                type: "log",
                seq: 1,
                workspace_id: workspaceId,
                stream_id: "active.stdout",
                source: "recovery",
                fd: "stdout",
                data: liveSecret,
                offset: 0,
                next_offset: liveSecret.length,
                occurred_at: now,
              },
            ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  const output = inspector.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);

  tailPhase = "deny_both";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();

  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(output).not.toContainText(activeMarker);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  const opensAtDenial = streamOpens;

  tailPhase = "recover_quiet";
  await inspector.getByRole("button", { name: "Tail", exact: true }).click();

  await expect(output).toContainText(quietRecovered, { timeout: 12_000 });
  await expect(output).not.toContainText(activeRecovered);
  await expect(inspector.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);

  tailPhase = "recover_active";
  activeRecoverRelease.resolve();
  await expect(output).toContainText(quietRecovered, { timeout: 12_000 });
  await expect(output).toContainText(activeRecovered);
  await expect(inspector.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAjDr: fullscreen
// loadSelectedTails must not treat a /logs/{stream} 401/403 as an ordinary
// tail entry. Listing stays reachable, so the column must clear cached tails
// and close its EventSource itself. A later listing 200 must not reopen
// /stream or let a queued live frame refill the revoked output, and must not
// wipe the tail-denial banner (PRRT_kwDOSJAM6s6gA1Lp).
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs close live stream after tail authorization denial while listing stays reachable (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailMode: "ok" | "denied" = "ok";
  let logsListingRequests = 0;
  const heldStream = createDeferred();
  const workspaceId = "ws_fs_tail_auth";
  const authorizedMarker = "authorized-fullscreen-tail-line";
  const liveSecret = "fullscreen-live-stream-after-tail-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      logsListingRequests += 1;
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      // Hold the already-open column EventSource until tail denial is applied,
      // then deliver a log frame. Listing stays 200, so the capability gate
      // and listing latch would otherwise keep /stream open.
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  tailMode = "denied";
  await modal.getByRole("button", { name: "Tail all" }).click();

  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect(modal.getByText(/stream idle/)).toBeVisible();

  // A later listing 200 must not wipe the tail-denial banner. The initial
  // wait is shorter than pollMs, so count a listing poll that finishes after
  // the banner is shown before asserting it is still explained.
  const listingPollsAtDenial = logsListingRequests;
  await expect.poll(() => logsListingRequests, { timeout: 12_000 }).toBeGreaterThan(listingPollsAtDenial);
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");

  heldStream.resolve();
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(output).not.toContainText(authorizedMarker);
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(modal.getByText(/stream idle/)).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gMwrf: a fullscreen
// /stream handshake or policy close that preserves 401/403 on the SSE frame
// must not be treated as a generic stream error. Previously authorized log
// text stays visible unless the column clears caches and holds /stream closed.
// A later listing or tail 200 is not recovery for that route.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs clear caches and hold the stream closed after stream authorization denial (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let streamOpens = 0;
  let logsListingRequests = 0;
  const heldStream = createDeferred();
  const workspaceId = "ws_fs_stream_auth";
  const authorizedMarker = "authorized-fullscreen-stream-line";
  const liveSecret = "fullscreen-live-stream-after-route-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      logsListingRequests += 1;
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
        {
          type: "error",
          error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
          message: "Workspace stream authorization denied.",
          status: deniedStatus,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  // Inspector and the fullscreen column both open /stream. Snapshot after
  // those connections exist so a later denial cannot hide a reconnect.
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensBeforeDenial = streamOpens;

  heldStream.resolve();
  await expect(modal.getByText(/Workspace stream authorization denied/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).not.toContainText(liveSecret);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  await expect(modal.getByText(/stream error/)).toHaveCount(0);
  await expect.poll(() => streamOpens).toBe(opensBeforeDenial);

  const listingPollsAtDenial = logsListingRequests;
  const opensAtDenial = streamOpens;
  await expect.poll(() => logsListingRequests, { timeout: 12_000 }).toBeGreaterThan(listingPollsAtDenial);
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(modal.getByText(/Workspace stream authorization denied/i)).toBeVisible();
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).not.toContainText(liveSecret);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 4_000 }).toBe(opensAtDenial);
  await expect(modal.getByText(/stream idle/)).toBeVisible();
});
}

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hRApz: once
// workspace_stream is withdrawn, a prior route-specific denial no longer
// applies to the still-supported polling-tail path. Release that latch and
// repopulate the cleared fullscreen column without reopening /stream.
test("fullscreen logs recover through tails when streaming is withdrawn after stream authorization denial", async ({
  page,
}) => {
  test.setTimeout(45_000);
  let capabilitiesRequests = 0;
  let streamAvailable = true;
  let streamOpens = 0;
  const heldStream = createDeferred();
  const capabilities = localCapabilities() as {
    diagnostics: Array<Record<string, unknown>>;
    [key: string]: unknown;
  };
  const withoutStreaming = {
    ...capabilities,
    diagnostics: capabilities.diagnostics.map((item) =>
      item.id === "workspace_stream"
        ? {
            id: item.id,
            availability: "unsupported",
            reason_code: "policy_disabled",
            message: "workspace_stream withdrawn",
            semantics: "Optional workspace live event/log stream.",
          }
        : item,
    ),
  };

  const api = await mockAwfApi(page);
  await page.route("**/api/awf/console/capabilities", async (route) => {
    capabilitiesRequests += 1;
    await fulfillJson(route, streamAvailable ? capabilities : withoutStreaming);
  });
  await page.route("**/api/awf/workspaces/ws_logs/stream**", async (route) => {
    streamOpens += 1;
    await heldStream.promise;
    const frame: AwfStreamFrame = {
      type: "error",
      error_code: "FORBIDDEN",
      message: "Workspace stream authorization denied.",
      status: 403,
    };
    await route.fulfill({
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
      },
      body: `data: ${JSON.stringify(frame)}\n\n`,
    });
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText("active line");
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);

  heldStream.resolve();
  await expect(modal.getByText(/Workspace stream authorization denied/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).toContainText("No log data loaded.");
  const tailReadsAtDenial = api.activeTailReads;
  const streamOpensAtDenial = streamOpens;
  const capabilitiesAtDenial = capabilitiesRequests;

  streamAvailable = false;
  await expect.poll(() => capabilitiesRequests, { timeout: 12_000 }).toBeGreaterThan(capabilitiesAtDenial);
  await expect.poll(() => api.activeTailReads, { timeout: 12_000 }).toBeGreaterThan(tailReadsAtDenial);
  await expect(output).toContainText("active line", { timeout: 12_000 });
  await expect(modal.getByText(/Workspace stream authorization denied/i)).toHaveCount(0);
  await expect(modal.getByText(/stream idle/i)).toBeVisible();
  await page.waitForTimeout(1_000);
  expect(streamOpens).toBe(streamOpensAtDenial);
});

// Regression for PR #958 review thread PRRT_kwDOSJAM6s6hRN-P: successful
// fullscreen /logs polls replace the streams array, but must not close and
// recreate an otherwise healthy EventSource.
streamTest("fullscreen logs keep a healthy stream open across listing polls", async ({
  page,
  openEventStream,
}) => {
  let streamOpens = 0;
  const streamUrl = await openEventStream([
    { type: "connected", workspace_id: "ws_logs" },
    { type: "heartbeat", workspace_id: "ws_logs" },
  ]);
  const api = await mockAwfApi(page);
  await page.route("**/api/awf/workspaces/ws_logs/stream**", async (route) => {
    streamOpens += 1;
    await route.continue({ url: streamUrl });
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByText(/stream live/)).toBeVisible({ timeout: 12_000 });
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensBeforeListingPoll = streamOpens;
  const listingPollsBefore = api.streamPolls;

  await expect.poll(() => api.streamPolls, { timeout: 12_000 }).toBeGreaterThan(listingPollsBefore);
  await page.waitForTimeout(1_000);
  expect(streamOpens).toBe(opensBeforeListingPoll);
  await expect(modal.getByText(/stream live/)).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gBlfk: fullscreen
// loadSelectedTails must not treat a sibling tail 200 as recovery while a
// previously denied stream retries with 5xx. /stream stays closed until that
// denied stream itself succeeds.
test("fullscreen logs keep tail denial latched until the denied stream recovers", async ({ page }) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "denied" | "sibling_success" | "recover" = "ok";
  let streamOpens = 0;
  const workspaceId = "ws_fs_tail_sibling_auth";
  const quietMarker = "authorized-quiet-fullscreen-tail";
  const activeMarker = "authorized-active-fullscreen-tail";
  const liveSecret = "fullscreen-denied-stream-live-frame-must-not-reappear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      if (tailPhase === "sibling_success") {
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "denied stream retry failed" } },
          503,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", activeMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] =
        tailPhase === "ok"
          ? [{ type: "connected", workspace_id: workspaceId }]
          : [
              { type: "connected", workspace_id: workspaceId },
              {
                type: "log",
                seq: 1,
                workspace_id: workspaceId,
                stream_id: "quiet.stdout",
                source: "monitor",
                fd: "stdout",
                data: liveSecret,
                offset: 0,
                next_offset: liveSecret.length,
                occurred_at: now,
              },
            ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);

  tailPhase = "denied";
  await modal.getByRole("button", { name: "Tail all" }).click();

  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  const opensAtDenial = streamOpens;

  tailPhase = "sibling_success";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  await expect(modal.getByText(liveSecret)).toHaveCount(0);

  tailPhase = "recover";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(output).toContainText(quietMarker, { timeout: 12_000 });
  await expect(modal.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCxOx: a denied tail
// that the operator deselects—or that leaves the latest listing—must not stay
// in the denial set and keep /stream closed after the remaining selected
// streams succeed. A denial that is still selected and listed still blocks.
test("fullscreen logs recover tail denial after the denied stream is deselected", async ({ page }) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "denied" | "remaining" = "ok";
  let listedStreamIds = ["quiet.stdout", "active.stdout"];
  let streamOpens = 0;
  const workspaceId = "ws_fs_tail_deselect_auth";
  const quietMarker = "authorized-quiet-fullscreen-tail";
  const activeMarker = "authorized-active-fullscreen-tail";
  const recoveredMarker = "remaining-selected-tail-after-denied-stream-deselected";
  const liveSecret = "fullscreen-denied-stream-live-frame-must-not-reappear-after-deselect";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(
        route,
        listEnvelope(listedStreamIds.map((streamId) =>
          logStream(
            streamId,
            streamId.startsWith("active") ? 2_880 : 2_400,
            120,
            streamId.startsWith("active") ? activeOpenedAt : quietOpenedAt,
          ),
        )),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase !== "ok") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          403,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(
        route,
        logRead("active.stdout", tailPhase === "remaining" ? recoveredMarker : activeMarker),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] =
        tailPhase === "ok"
          ? [{ type: "connected", workspace_id: workspaceId }]
          : [
              { type: "connected", workspace_id: workspaceId },
              {
                type: "log",
                seq: 1,
                workspace_id: workspaceId,
                stream_id: "quiet.stdout",
                source: "monitor",
                fd: "stdout",
                data: liveSecret,
                offset: 0,
                next_offset: liveSecret.length,
                occurred_at: now,
              },
            ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);

  tailPhase = "denied";
  await modal.getByRole("button", { name: "Tail all" }).click();

  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  const opensAtDenial = streamOpens;

  // The denied stream is still selected. A successful sibling read must not
  // drop its latch or reopen /stream.
  tailPhase = "remaining";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);

  await modal.getByRole("checkbox", { name: "quiet.stdout" }).uncheck();
  await expect(output).toContainText(recoveredMarker, { timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(modal.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);

  // A denied stream that vanishes from the listing must also stop blocking
  // the streams that remain selected.
  listedStreamIds = ["quiet.stdout", "active.stdout"];
  tailPhase = "denied";
  await modal.getByRole("checkbox", { name: "quiet.stdout" }).check();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).toContainText("No log data loaded.");
  const opensAfterReselectDenial = streamOpens;

  tailPhase = "remaining";
  listedStreamIds = ["active.stdout"];
  await expect(modal.getByRole("checkbox", { name: "quiet.stdout" })).toHaveCount(0, { timeout: 12_000 });
  await expect(output).toContainText(recoveredMarker, { timeout: 12_000 });
  await expect(modal.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAfterReselectDenial);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCA3H: a fullscreen
// /logs/{stream} 401/403 must clear cached tails and close EventSource without
// waiting for a sibling read that has not settled. Promise.all made revocation
// contingent on every selected tail completing.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs apply tail denial without waiting for a hanging sibling (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "split" = "ok";
  let streamOpens = 0;
  const hangingSibling = createDeferred();
  const heldStream = createDeferred();
  const workspaceId = "ws_fs_tail_hanging_sibling";
  const deniedMarker = "authorized-denied-fullscreen-tail";
  const siblingMarker = "authorized-hanging-fullscreen-tail";
  const siblingAfterDenial = "hanging-sibling-success-must-not-restore-fullscreen-tail";
  const liveSecret = "fullscreen-live-frame-before-hanging-sibling-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "split") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log tail permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", deniedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "split") {
        await hangingSibling.promise;
        await fulfillJson(route, logRead("active.stdout", siblingAfterDenial));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", siblingMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "quiet.stdout",
          source: "monitor",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(deniedMarker);
  await expect(output).toContainText(siblingMarker);

  tailPhase = "split";
  await modal.getByRole("button", { name: "Tail all" }).click();

  // The sibling tail is still pending. Denial must already have cleared caches
  // and closed /stream; waiting for Promise.all would leave the prior snapshot.
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(deniedMarker);
  await expect(output).not.toContainText(siblingMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  const opensAtDenial = streamOpens;

  hangingSibling.resolve();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).not.toContainText(siblingAfterDenial);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);

  heldStream.resolve();
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(modal.getByText(/stream idle/)).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCSz-: start a fullscreen
// tail reload, then a newer manual reload, before the first returns 401/403.
// Discarding that denial because tailRequestGenerationRef already moved leaves
// cached tails and EventSource open if the newer request hangs or fails
// transiently. A later request that starts after the denial may still recover.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs apply tail denial while a newer reload is in flight (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "hold" | "recover" = "ok";
  let olderStarted = 0;
  let newerStarted = 0;
  let streamOpens = 0;
  const heldTails: Array<{ promise: Promise<void>; resolve: () => void }> = [];
  const heldStream = createDeferred();
  const workspaceId = "ws_fs_tail_slow_denial";
  const authorizedMarker = "authorized-before-slow-tail-denial";
  const recoveryMarker = "recovered-after-slow-tail-denial";
  const liveSecret = "live-frame-during-slow-tail-denial-must-not-appear";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "hold") {
        const index = heldTails.length;
        const gate = createDeferred();
        heldTails.push(gate);
        if (index === 0) {
          olderStarted = 1;
        } else {
          newerStarted += 1;
        }
        await gate.promise;
        if (index === 0) {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "log tail permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(route, { detail: { message: "transient tail refresh failed" } }, 500);
        return;
      }
      if (tailPhase === "recover") {
        await fulfillJson(route, logRead("active.stdout", recoveryMarker));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);

  tailPhase = "hold";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => olderStarted, { timeout: 12_000 }).toBe(1);
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => newerStarted, { timeout: 12_000 }).toBe(1);

  const older = heldTails[0];
  const newer = heldTails[1];
  expect(older, "held older tail denial").toBeTruthy();
  expect(newer, "held newer tail reload").toBeTruthy();
  older?.resolve();

  // The newer reload is still pending. Denial must already have cleared caches
  // and closed /stream; waiting for that newer request would leave the snapshot.
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  const opensAtDenial = streamOpens;

  newer?.resolve();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(modal.getByText(/stream idle/)).toBeVisible();

  heldStream.resolve();
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(1_000);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();

  tailPhase = "recover";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(output).toContainText(recoveryMarker, { timeout: 12_000 });
  await expect(modal.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gK-GS: a newer
// loadSelectedTails wave can apply a 200 for stream A while B fails
// transiently. That must not advance a global success generation that
// discards an older in-flight 401/403 for B. Only a newer success for the
// denied stream itself may suppress its denial.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs apply tail denial when only a sibling stream has a newer success (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "hold" = "ok";
  let quietWaves = 0;
  let activeWaves = 0;
  let streamOpens = 0;
  const heldTails = new Map<string, { promise: Promise<void>; resolve: () => void }>();
  const heldStream = createDeferred();
  const workspaceId = "ws_fs_tail_sibling_success_denial";
  const quietMarker = "cached-quiet-tail-before-sibling-success";
  const activeMarker = "cached-active-tail-before-sibling-success";
  const activeNewerMarker = "newer-active-tail-must-not-hide-sibling-denial";
  const liveSecret = "live-frame-after-sibling-success-must-not-hide-denial";

  const holdTail = (streamId: string, wave: number) => {
    const key = `${streamId}:${wave}`;
    const existing = heldTails.get(key);
    if (existing) {
      return existing;
    }
    const gate = createDeferred();
    heldTails.set(key, gate);
    return gate;
  };

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(
        route,
        listEnvelope([
          logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
          logStream("active.stdout", 2_880, 120, activeOpenedAt),
        ]),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "hold") {
        const wave = quietWaves;
        quietWaves += 1;
        await holdTail("quiet.stdout", wave).promise;
        if (wave === 0) {
          await fulfillJson(
            route,
            {
              detail: {
                error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
                message: "log tail permission revoked",
              },
            },
            deniedStatus,
          );
          return;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: "transient quiet tail failed" } },
          503,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", quietMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "hold") {
        const wave = activeWaves;
        activeWaves += 1;
        await holdTail("active.stdout", wave).promise;
        if (wave === 0) {
          await fulfillJson(route, logRead("active.stdout", activeMarker));
          return;
        }
        await fulfillJson(route, logRead("active.stdout", activeNewerMarker));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", activeMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      await heldStream.promise;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "quiet.stdout",
          source: "monitor",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(quietMarker);
  await expect(output).toContainText(activeMarker);

  tailPhase = "hold";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => quietWaves, { timeout: 12_000 }).toBe(1);
  await expect.poll(() => activeWaves, { timeout: 12_000 }).toBe(1);

  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => quietWaves, { timeout: 12_000 }).toBe(2);
  await expect.poll(() => activeWaves, { timeout: 12_000 }).toBe(2);

  // Newer wave: A succeeds, B fails transiently. The aggregate path applies
  // A's snapshot before the older wave's 401/403 for B settles.
  heldTails.get("active.stdout:1")?.resolve();
  heldTails.get("quiet.stdout:1")?.resolve();
  await expect(output).toContainText(activeNewerMarker, { timeout: 12_000 });
  await expect(output).toContainText(quietMarker);
  await expect(output).not.toContainText(activeMarker);

  heldTails.get("quiet.stdout:0")?.resolve();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(quietMarker);
  await expect(output).not.toContainText(activeNewerMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  const opensAtDenial = streamOpens;

  heldTails.get("active.stdout:0")?.resolve();
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(output).not.toContainText(quietMarker);
  await expect(output).not.toContainText(activeMarker);
  await expect(output).not.toContainText(activeNewerMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens, { timeout: 3_000 }).toBe(opensAtDenial);
  await expect(modal.getByText(/stream idle/)).toBeVisible();

  heldStream.resolve();
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(1_000);
  await expect(modal.getByText(liveSecret)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
  await expect(modal.getByText(/log tail permission revoked/i)).toBeVisible();
  await expect(modal.getByText(/stream idle/)).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCRNH: a tail 401/403
// that started before a listing denial must not revoke the column after that
// listing recovers. Listing denial bumps the column epoch and clears
// listingDenied on recovery while the original tail wave can still be in
// flight, so the denial discard guard has to compare the captured epoch.
for (const deniedStatus of [401, 403] as const) {
test(`fullscreen logs ignore a stale tail denial after listing recovery (${deniedStatus})`, async ({
  page,
}) => {
  test.setTimeout(60_000);
  let listingMode: "ok" | "denied" | "recover" = "ok";
  let holdNextTail = false;
  let tailRequestId = 0;
  let heldTailId = 0;
  let staleTailSettled = 0;
  let streamOpens = 0;
  const staleTailHold = createDeferred();
  const recoveredTailHold = createDeferred();
  const workspaceId = "ws_fs_stale_tail_after_listing";
  const authorizedMarker = "authorized-before-stale-tail-denial";
  const recoveredMarker = "listing-recovered-tail-must-stay";
  const liveSecret = "live-frame-after-stale-tail-denial-must-not-be-required";

  await page.addInitScript(() => {
    const pageWindow = window as Window & {
      __awfArmStaleTail?: () => void;
      __awfReleaseStaleTailOnNextListing?: () => void;
    };
    const originalFetch = window.fetch.bind(window);
    let armed = false;
    let releaseOnNextListingOk = false;
    let releaseStaleTail = () => undefined;
    let staleTailGate = Promise.resolve();
    pageWindow.__awfArmStaleTail = () => {
      armed = true;
      staleTailGate = new Promise<void>((resolve) => {
        releaseStaleTail = () => {
          resolve();
        };
      });
    };
    pageWindow.__awfReleaseStaleTailOnNextListing = () => {
      releaseOnNextListingOk = true;
    };
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const response = await originalFetch(input, init);
      const isTail = url.includes("/logs/");
      const isListing = /\/logs(?:\?|$)/.test(url);
      if (armed && isTail) {
        await staleTailGate;
      }
      if (armed && isListing && response.ok && releaseOnNextListingOk) {
        releaseOnNextListingOk = false;
        const readBody = response.text.bind(response);
        response.text = async () => {
          const text = await readBody();
          // parseApiResponse, then loadStreams, clear listingDenied before
          // React effects assign a new tail generation. Release the pre-denial
          // 401 in that gap.
          queueMicrotask(() => {
            queueMicrotask(() => {
              queueMicrotask(() => {
                releaseStaleTail();
              });
            });
          });
          return text;
        };
      }
      return response;
    };
  });

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "log listing permission revoked",
            },
          },
          deniedStatus,
        );
        return;
      }
      if (listingMode === "recover") {
        // Put the 401 on the wire first. The page fetch wrapper holds it
        // until this listing 200 is handed back to loadStreams.
        staleTailHold.resolve();
        const started = Date.now();
        while (staleTailSettled === 0 && Date.now() - started < 5_000) {
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      const requestId = ++tailRequestId;
      if (holdNextTail && heldTailId === 0) {
        heldTailId = requestId;
        await staleTailHold.promise;
        await fulfillJson(
          route,
          {
            detail: {
              error_code: deniedStatus === 401 ? "UNAUTHORIZED" : "FORBIDDEN",
              message: "stale tail permission revoked",
            },
          },
          deniedStatus,
        );
        staleTailSettled += 1;
        return;
      }
      if (listingMode === "recover") {
        await recoveredTailHold.promise;
        await fulfillJson(route, logRead("active.stdout", recoveredMarker));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: workspaceId },
        {
          type: "log",
          seq: streamOpens,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  holdNextTail = true;
  await page.evaluate(() => {
    (window as Window & { __awfArmStaleTail?: () => void }).__awfArmStaleTail?.();
  });
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => heldTailId, { timeout: 12_000 }).toBeGreaterThan(0);

  listingMode = "denied";
  await expect(modal.getByText("log listing permission revoked")).toBeVisible({ timeout: 12_000 });
  await expect(modal.getByText("No log streams recorded.")).toBeVisible();
  await expect(output).toContainText("No log data loaded.");

  const opensAtDenial = streamOpens;
  await page.evaluate(() => {
    (window as Window & { __awfReleaseStaleTailOnNextListing?: () => void }).__awfReleaseStaleTailOnNextListing?.();
  });
  listingMode = "recover";

  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible({ timeout: 12_000 });
  await expect(modal.getByText("log listing permission revoked")).toHaveCount(0);
  await expect.poll(() => staleTailSettled, { timeout: 12_000 }).toBe(1);
  // The stale 401 must not latch after listingDenied is cleared. A later
  // recovered tail 200 would hide that latch, so assert before releasing it.
  await expect(modal.getByText("stale tail permission revoked")).toHaveCount(0);
  await page.waitForTimeout(1_000);
  await expect(modal.getByText("stale tail permission revoked")).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(opensAtDenial);

  recoveredTailHold.resolve();
  await expect(output).toContainText(recoveredMarker, { timeout: 12_000 });
  await expect(modal.getByText("stale tail permission revoked")).toHaveCount(0);
  await expect(modal.getByText(/log tail permission revoked/i)).toHaveCount(0);
  await expect(output).not.toContainText("No log data loaded.");
  await expect(modal.getByText(/stream idle/)).toHaveCount(0);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gAqk-: a fullscreen
// /logs/{stream} network or 5xx failure must keep the last successful tail
// snapshot and show a separate refresh warning. It must not replace the
// snapshot with the synthetic error entry from a failed tail read.
for (const outageStatus of [0, 503] as const) {
test(`fullscreen logs retain last-successful tails on transient tail refresh failure (${outageStatus === 0 ? "network" : outageStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailMode: "ok" | "outage" = "ok";
  const workspaceId = "ws_fs_tail_outage";
  const retainedMarker = "retained-fullscreen-tail-line";
  const outageMessage = "fullscreen tail feed outage";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailMode === "outage") {
        if (outageStatus === 0) {
          await route.abort("failed");
          return;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          outageStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("active.stdout", retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);

  tailMode = "outage";
  await modal.getByRole("button", { name: "Tail all" }).click();

  const refreshWarning = modal.getByRole("alert");
  await expect(refreshWarning).toContainText(outageStatus === 0 ? /unable to load log stream/i : outageMessage, {
    timeout: 12_000,
  });
  await expect(output).toContainText(retainedMarker);
  await expect(output).not.toContainText("Unable to load log stream");
  await expect(modal.locator("[data-awf-stale='true']")).toBeVisible();
  await expect(modal.getByTitle("Showing the last snapshot — live data may be stale")).toBeVisible();

  tailMode = "ok";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(refreshWarning).toHaveCount(0);
  await expect(output).toContainText(retainedMarker);
  await expect(modal.locator("[data-awf-stale='true']")).toHaveCount(0);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gIJVJ: a network/5xx
// tail read leaves the 401/403 denial set empty. A static or closed listing
// does not change the tail fingerprint, so later polls used to skip the
// refresh and the stale snapshot warning persisted until Tail all.
for (const outageStatus of [0, 503] as const) {
test(`fullscreen logs retry a static tail refresh failure on unchanged listing and recover (${outageStatus === 0 ? "network" : outageStatus})`, async ({
  page,
}) => {
  test.setTimeout(60_000);
  let tailMode: "ok" | "outage" | "recover" = "ok";
  let listingPolls = 0;
  let tailReads = 0;
  const workspaceId = "ws_fs_tail_outage_static_retry";
  const retainedMarker = "retained-static-fullscreen-tail-line";
  const recoveredMarker = "recovered-static-fullscreen-tail-line";
  const outageMessage = "static fullscreen tail feed outage";
  const closedAt = "2026-05-21T10:05:00.000Z";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      listingPolls += 1;
      await fulfillJson(route, listEnvelope([{
        ...logStream("closed.stdout", 2_880, 120, activeOpenedAt),
        closed_at: closedAt,
      }]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/closed.stdout`) {
      tailReads += 1;
      if (tailMode === "outage") {
        if (outageStatus === 0) {
          await route.abort("failed");
          return;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          outageStatus,
        );
        return;
      }
      await fulfillJson(
        route,
        logRead("closed.stdout", tailMode === "recover" ? recoveredMarker : retainedMarker),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);

  tailMode = "outage";
  await modal.getByRole("button", { name: "Tail all" }).click();

  const refreshWarning = modal.getByRole("alert");
  await expect(refreshWarning).toContainText(outageStatus === 0 ? /unable to load log stream/i : outageMessage, {
    timeout: 12_000,
  });
  await expect(output).toContainText(retainedMarker);
  await expect(modal.locator("[data-awf-stale='true']")).toBeVisible();

  const readsAtOutage = tailReads;
  const listingAtOutage = listingPolls;
  expect(readsAtOutage).toBeGreaterThan(0);

  await page.waitForTimeout(400);
  expect(tailReads - readsAtOutage).toBeLessThanOrEqual(1);

  tailMode = "recover";
  await expect.poll(() => listingPolls, { timeout: 20_000 }).toBeGreaterThan(listingAtOutage);
  await expect.poll(() => tailReads, { timeout: 20_000 }).toBeGreaterThan(readsAtOutage);
  await expect(output).toContainText(recoveredMarker, { timeout: 12_000 });
  await expect(refreshWarning).toHaveCount(0);
  await expect(modal.locator("[data-awf-stale='true']")).toHaveCount(0);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gIJVF: a fullscreen
// network/5xx tail failure must copy into the refresh warning as soon as that
// read settles. Promise.all never reaches the batch copy while a sibling tail
// hangs, so waiting for apiGet's deadline would keep the last snapshot in the
// column too long with no stale/error warning.
for (const outageStatus of [0, 503] as const) {
test(`fullscreen logs surface a tail refresh failure without waiting for a hanging sibling (${outageStatus === 0 ? "network" : outageStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let tailPhase: "ok" | "split" = "ok";
  const hangingSibling = createDeferred();
  const workspaceId = "ws_fs_tail_outage_hanging_sibling";
  const retainedMarker = "retained-fullscreen-tail-during-sibling-hang";
  const siblingMarker = "authorized-hanging-fullscreen-tail";
  const siblingAfterOutage = "sibling-tail-after-outage-settled";
  const outageMessage = "fullscreen tail feed outage while sibling hangs";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (tailPhase === "split") {
        if (outageStatus === 0) {
          await route.abort("failed");
          return;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          outageStatus,
        );
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailPhase === "split") {
        await hangingSibling.promise;
        await fulfillJson(route, logRead("active.stdout", siblingAfterOutage));
        return;
      }
      await fulfillJson(route, logRead("active.stdout", siblingMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);
  await expect(output).toContainText(siblingMarker);

  tailPhase = "split";
  await modal.getByRole("button", { name: "Tail all" }).click();

  // The sibling tail is still pending. The settled outage must already warn
  // and keep the last snapshot; waiting for Promise.all would leave neither.
  const refreshWarning = modal.getByRole("alert");
  await expect(refreshWarning).toContainText(outageStatus === 0 ? /unable to load log stream/i : outageMessage, {
    timeout: 12_000,
  });
  await expect(output).toContainText(retainedMarker);
  await expect(output).toContainText(siblingMarker);
  await expect(output).not.toContainText("Unable to load log stream");
  await expect(modal.locator("[data-awf-stale='true']")).toBeVisible();
  await expect(modal.getByTitle("Showing the last snapshot — live data may be stale")).toBeVisible();

  hangingSibling.resolve();
  await expect(output).toContainText(siblingAfterOutage, { timeout: 12_000 });
  await expect(output).toContainText(retainedMarker);
  await expect(refreshWarning).toContainText(outageStatus === 0 ? /unable to load log stream/i : outageMessage);
  await expect(output).not.toContainText("Unable to load log stream");
  await expect(modal.locator("[data-awf-stale='true']")).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gCxO0: a latched
// tail 401/403 shares the column error banner with listing failures. A later
// transient /logs outage must not replace that authorization reason, and a
// subsequent listing 200 must not leave the recovered outage on screen —
// listing success refuses to clear the banner while the tail denial remains.
test("fullscreen logs keep latched tail denial through a listing outage", async ({ page }) => {
  test.setTimeout(45_000);
  let tailMode: "ok" | "denied" = "ok";
  let listingMode: "ok" | "outage" = "ok";
  let listingOutages = 0;
  let listingRecoveries = 0;
  const workspaceId = "ws_fs_tail_auth_listing_outage";
  const authorizedMarker = "authorized-fullscreen-tail-before-denial";
  const denialMessage = "log tail permission revoked";
  const outageMessage = "transient log listing outage";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "outage") {
        listingOutages += 1;
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          503,
        );
        return;
      }
      if (listingOutages > 0) {
        listingRecoveries += 1;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      if (tailMode === "denied") {
        await fulfillJson(
          route,
          {
            detail: {
              error_code: "FORBIDDEN",
              message: denialMessage,
            },
          },
          403,
        );
        return;
      }
      await fulfillJson(route, logRead("active.stdout", authorizedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(authorizedMarker);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  tailMode = "denied";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(modal.getByText(denialMessage)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");

  listingMode = "outage";
  await expect.poll(() => listingOutages, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(denialMessage)).toBeVisible();
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");

  listingMode = "ok";
  await expect.poll(() => listingRecoveries, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect(modal.getByText(denialMessage)).toBeVisible();
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(output).not.toContainText(authorizedMarker);
  await expect(output).toContainText("No log data loaded.");
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gK-GY: a successful
// tail read must not clear a listing network/5xx banner. Tail all would
// otherwise drop the outage while last-good stream metadata stays up, and a
// hung follow-up listing would present that inventory as current.
test("fullscreen logs preserve listing outage across a successful tail", async ({ page }) => {
  test.setTimeout(45_000);
  let listingMode: "ok" | "outage" | "hang" = "ok";
  let listingOutages = 0;
  let listingRecoveries = 0;
  const hangingListings: Array<() => void> = [];
  const workspaceId = "ws_fs_listing_outage_tail_clear";
  const streamId = "active.stdout";
  const retainedMarker = "retained-tail-before-listing-outage";
  const tailedAfterOutage = "tail-succeeded-during-listing-outage";
  const outageMessage = "log listing feed outage during tail";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "hang") {
        await new Promise<void>((resolve) => {
          hangingListings.push(resolve);
        });
        listingRecoveries += 1;
        await fulfillJson(route, listEnvelope([logStream(streamId, 2_880, 120, activeOpenedAt)]));
        return;
      }
      if (listingMode === "outage") {
        listingOutages += 1;
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          503,
        );
        return;
      }
      await fulfillJson(route, listEnvelope([logStream(streamId, 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/${streamId}`) {
      await fulfillJson(
        route,
        logRead(streamId, listingOutages > 0 ? tailedAfterOutage : retainedMarker),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);
  await expect(modal.getByRole("checkbox", { name: streamId })).toBeVisible();

  listingMode = "outage";
  await expect.poll(() => listingOutages, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(outageMessage)).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: streamId })).toBeVisible();
  await expect(output).toContainText(retainedMarker);

  listingMode = "hang";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect(output).toContainText(tailedAfterOutage, { timeout: 12_000 });
  await expect.poll(() => hangingListings.length, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(outageMessage)).toBeVisible();
  await expect(modal.getByRole("checkbox", { name: streamId })).toBeVisible();
  await expect(output).toContainText(tailedAfterOutage);
  await expect(output).not.toContainText("No log data loaded.");

  listingMode = "ok";
  while (hangingListings.length > 0) {
    hangingListings.shift()?.();
  }
  await expect.poll(() => listingRecoveries, { timeout: 12_000 }).toBeGreaterThan(0);
  // The inspector and fullscreen poll independently. The recovered request
  // counted above may belong to the inspector; allow the fullscreen's next
  // 5s poll and response to settle rather than timing out at the poll boundary.
  await expect(modal.getByText(outageMessage)).toHaveCount(0, { timeout: 12_000 });
  await expect(modal.getByRole("checkbox", { name: streamId })).toBeVisible();
  await expect(output).toContainText(tailedAfterOutage);
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gOqTl: a later /stream
// handshake must not drop the newest listing outage stored while the stream-
// auth latch owned the banner. A hanging listing now settles at apiGet's
// deadline before the delayed stream probe, so probe recovery must surface
// that newer timeout or last-good streams look current.
test("fullscreen logs restore latest listing outage after stream probe recovery", async ({ page }) => {
  test.setTimeout(60_000);
  let streamPhase: "deny" | "probe" = "deny";
  let listingMode: "ok" | "outage" | "hang" = "ok";
  let listingOutages = 0;
  let listingRecoveries = 0;
  let streamOpens = 0;
  const heldStream = createDeferred();
  const hangingListings: Array<() => void> = [];
  const workspaceId = "ws_fs_stream_probe_listing_outage";
  const retainedMarker = "retained-tail-before-stream-denial";
  const denialMessage = "Workspace stream authorization denied.";
  const outageMessage = "log listing outage during stream probe";
  const deadlineOutageMessage = /Console API request timed out after \d+ms/;

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      if (listingMode === "hang") {
        await new Promise<void>((resolve) => {
          hangingListings.push(resolve);
        });
        listingRecoveries += 1;
        await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
        return;
      }
      if (listingMode === "outage") {
        listingOutages += 1;
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          503,
        );
        return;
      }
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      if (streamPhase === "deny") {
        await heldStream.promise;
        const frames: AwfStreamFrame[] = [
          { type: "connected", workspace_id: workspaceId },
          {
            type: "error",
            error_code: "FORBIDDEN",
            message: denialMessage,
            status: 403,
          },
        ];
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
          },
          body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensBeforeDenial = streamOpens;

  heldStream.resolve();
  await expect(modal.getByText(denialMessage)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(retainedMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens).toBe(opensBeforeDenial);

  listingMode = "outage";
  streamPhase = "probe";
  await expect.poll(() => listingOutages, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(denialMessage)).toBeVisible();
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();

  // Hold the follow-up listing so a later 503 cannot re-apply the banner
  // after the probe clears the latch. Only acceptStreamProbe can restore it.
  listingMode = "hang";
  await expect.poll(() => hangingListings.length, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensAtOutage = streamOpens;
  await expect.poll(() => streamOpens, { timeout: 30_000 }).toBeGreaterThan(opensAtOutage);
  await expect(modal.getByText(deadlineOutageMessage)).toBeVisible({ timeout: 12_000 });
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(modal.getByText(denialMessage)).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
  await expect(output).toContainText("No log data loaded.");

  listingMode = "ok";
  while (hangingListings.length > 0) {
    hangingListings.shift()?.();
  }
  await expect.poll(() => listingRecoveries, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByText(outageMessage)).toHaveCount(0);
  await expect(modal.getByText(deadlineOutageMessage)).toHaveCount(0);
  await expect(modal.getByRole("checkbox", { name: "active.stdout" })).toBeVisible();
});

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gPpu9: a delayed inspector
// /stream probe that receives a non-auth error or closed frame must close that
// EventSource and schedule another probe. Programmatic close() does not fire
// onerror, so leaving the route latch and a nonzero nonce blanks the inspector
// after authorization recovers. A later heartbeat or log on the failed
// connection must not accept the probe.
for (const terminal of ["error", "closed"] as const) {
streamTest(`inspector reschedules a stream probe after a non-auth ${terminal} frame`, async ({ page, openEventStream }) => {
  test.setTimeout(90_000);
  let streamPhase: "deny" | "probe-fail" | "recover" = "deny";
  let streamOpens = 0;
  const heldStream = createDeferred();
  const workspaceId = `ws_inspector_stream_probe_${terminal}`;
  const recoveryStreamUrl = await openEventStream([
    { type: "connected", workspace_id: workspaceId },
    { type: "heartbeat", workspace_id: workspaceId },
  ]);
  const liveSecret = "inspector-probe-log-after-terminal-frame-must-not-appear";
  const denialMessage = "Workspace stream authorization denied.";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", "authorized-inspector-before-stream-probe"));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      if (streamPhase === "deny") {
        await heldStream.promise;
        const frames: AwfStreamFrame[] = [
          { type: "connected", workspace_id: workspaceId },
          {
            type: "error",
            error_code: "FORBIDDEN",
            message: denialMessage,
            status: 403,
          },
        ];
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
          },
          body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
        });
        return;
      }
      if (streamPhase === "recover") {
        await route.continue({ url: recoveryStreamUrl });
        return;
      }
      const terminalFrame: AwfStreamFrame =
        terminal === "error"
          ? {
              type: "error",
              error_code: "UPSTREAM_UNAVAILABLE",
              message: "stream probe failed without authorization",
              status: 503,
            }
          : {
              type: "closed",
              workspace_id: workspaceId,
              code: 1000,
              reason: "stream probe closed without authorization",
            };
      const frames: AwfStreamFrame[] = [
        terminalFrame,
        { type: "heartbeat", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto(`/?workspaceId=${workspaceId}`);
  await waitForConsoleReady(page);

  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector).toHaveClass(/translate-x-0/);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensBeforeDenial = streamOpens;

  heldStream.resolve();
  await expect(inspector.getByText(denialMessage)).toBeVisible({ timeout: 12_000 });
  await expect(page.getByText("Stream: idle")).toBeVisible();
  await expect.poll(() => streamOpens).toBe(opensBeforeDenial);

  streamPhase = "probe-fail";
  const opensAtDenial = streamOpens;
  await expect.poll(() => streamOpens, { timeout: 30_000 }).toBeGreaterThan(opensAtDenial);
  await expect(inspector.getByText(denialMessage)).toBeVisible();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(page.getByText("Stream: live")).toHaveCount(0);
  await expect(page.getByText("Stream: idle")).toBeVisible();

  const opensAfterFailedProbe = streamOpens;
  await expect.poll(() => streamOpens, { timeout: 4_000 }).toBe(opensAfterFailedProbe);
  streamPhase = "recover";
  await expect.poll(() => streamOpens, { timeout: 30_000 }).toBeGreaterThan(opensAfterFailedProbe);
  await expect(inspector.getByText(denialMessage)).toHaveCount(0, { timeout: 12_000 });
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(page.getByText("Stream: live")).toBeVisible();
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gOqTv: a delayed /stream
// probe that receives a non-auth error or closed frame must close that
// EventSource. A later heartbeat or log on the same connection must not
// accept the probe and treat the route as recovered.
for (const terminal of ["error", "closed"] as const) {
test(`fullscreen logs do not recover a stream probe after a non-auth ${terminal} frame`, async ({
  page,
}) => {
  test.setTimeout(60_000);
  let streamPhase: "deny" | "probe-fail" = "deny";
  let streamOpens = 0;
  const heldStream = createDeferred();
  const workspaceId = `ws_fs_stream_probe_${terminal}`;
  const retainedMarker = "retained-tail-before-failed-stream-probe";
  const liveSecret = "probe-log-after-terminal-frame-must-not-appear";
  const denialMessage = "Workspace stream authorization denied.";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([logStream("active.stdout", 2_880, 120, activeOpenedAt)]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(route, logRead("active.stdout", retainedMarker));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      streamOpens += 1;
      if (streamPhase === "deny") {
        await heldStream.promise;
        const frames: AwfStreamFrame[] = [
          { type: "connected", workspace_id: workspaceId },
          {
            type: "error",
            error_code: "FORBIDDEN",
            message: denialMessage,
            status: 403,
          },
        ];
        await route.fulfill({
          status: 200,
          headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache",
          },
          body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
        });
        return;
      }
      const terminalFrame: AwfStreamFrame =
        terminal === "error"
          ? {
              type: "error",
              error_code: "UPSTREAM_UNAVAILABLE",
              message: "stream probe failed without authorization",
              status: 503,
            }
          : {
              type: "closed",
              workspace_id: workspaceId,
              code: 1000,
              reason: "stream probe closed without authorization",
            };
      const frames: AwfStreamFrame[] = [
        terminalFrame,
        { type: "heartbeat", workspace_id: workspaceId },
        {
          type: "log",
          seq: 1,
          workspace_id: workspaceId,
          stream_id: "active.stdout",
          source: "agent",
          fd: "stdout",
          data: liveSecret,
          offset: 0,
          next_offset: liveSecret.length,
          occurred_at: now,
        },
      ];
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedMarker);
  await expect.poll(() => streamOpens, { timeout: 12_000 }).toBeGreaterThan(0);
  const opensBeforeDenial = streamOpens;

  heldStream.resolve();
  await expect(modal.getByText(denialMessage)).toBeVisible({ timeout: 12_000 });
  await expect(output).not.toContainText(retainedMarker);
  await expect(output).toContainText("No log data loaded.");
  await expect.poll(() => streamOpens).toBe(opensBeforeDenial);

  streamPhase = "probe-fail";
  const opensAtDenial = streamOpens;
  await expect.poll(() => streamOpens, { timeout: 30_000 }).toBeGreaterThan(opensAtDenial);
  await expect(modal.getByText(denialMessage)).toBeVisible();
  await expect(modal.getByText(/stream live/)).toHaveCount(0);
  await expect(modal.getByText(/stream idle/)).toBeVisible();
  await expect(output).not.toContainText(liveSecret);
  await expect(output).not.toContainText(retainedMarker);
  await expect(output).toContainText("No log data loaded.");

  const opensAfterFailedProbe = streamOpens;
  await expect.poll(() => streamOpens, { timeout: 4_000 }).toBe(opensAfterFailedProbe);
  await expect(modal.getByText(denialMessage)).toBeVisible();
  await expect(output).not.toContainText(liveSecret);
});
}

// Regression for PR #933 review thread PRRT_kwDOSJAM6s6gNmx0: a newer success
// for stream A advances the global applied tail generation. An older
// network/5xx for stream B must still warn after the operator deselects B,
// an A-only wave completes, and B is reselected while its new read hangs.
for (const outageStatus of [0, 503] as const) {
test(`fullscreen logs keep a stream outage after a sibling tail success (${outageStatus === 0 ? "network" : outageStatus})`, async ({
  page,
}) => {
  test.setTimeout(45_000);
  let quietPhase: "ok" | "hold-fail" | "hang" = "ok";
  let activePhase: "ok" | "advance" = "ok";
  const heldQuietFailures: Array<() => void> = [];
  const hangingReselectedQuiet: Array<() => void> = [];
  const workspaceId = "ws_fs_tail_outage_sibling_success";
  const retainedQuiet = "retained-quiet-tail-during-sibling-success";
  const retainedActive = "retained-active-tail-before-a-only-wave";
  const advancedActive = "active-tail-after-quiet-deselected";
  const outageMessage = "quiet tail outage after sibling success";

  await page.route("**/api/awf/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, localDashboardSummary());
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverviewFor(workspaceId)]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/runtime`) {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/events`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/operations`) {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}`) {
      await fulfillJson(route, workspaceOverviewFor(workspaceId));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs`) {
      await fulfillJson(route, listEnvelope([
        logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
        logStream("active.stdout", 2_880, 120, activeOpenedAt),
      ]));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/quiet.stdout`) {
      if (quietPhase === "hold-fail") {
        await new Promise<void>((resolve) => {
          heldQuietFailures.push(resolve);
        });
        if (outageStatus === 0) {
          await route.abort("failed");
          return;
        }
        await fulfillJson(
          route,
          { detail: { error_code: "UPSTREAM_UNAVAILABLE", message: outageMessage } },
          outageStatus,
        );
        return;
      }
      if (quietPhase === "hang") {
        await new Promise<void>((resolve) => {
          hangingReselectedQuiet.push(resolve);
        });
        await fulfillJson(route, logRead("quiet.stdout", retainedQuiet));
        return;
      }
      await fulfillJson(route, logRead("quiet.stdout", retainedQuiet));
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/logs/active.stdout`) {
      await fulfillJson(
        route,
        logRead("active.stdout", activePhase === "advance" ? advancedActive : retainedActive),
      );
      return;
    }
    if (path === `/api/awf/workspaces/${workspaceId}/stream`) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
      return;
    }
    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });

  await page.goto("/");
  await waitForConsoleReady(page);
  await page.getByTestId(`workspace-card-${workspaceId}`).getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  const output = modal.getByTestId("log-output");
  await expect(output).toContainText(retainedQuiet);
  await expect(output).toContainText(retainedActive);

  quietPhase = "hold-fail";
  await modal.getByRole("button", { name: "Tail all" }).click();
  await expect.poll(() => heldQuietFailures.length, { timeout: 12_000 }).toBeGreaterThan(0);

  activePhase = "advance";
  await modal.getByRole("checkbox", { name: "quiet.stdout" }).uncheck();
  await expect(output).toContainText(advancedActive, { timeout: 12_000 });
  await expect(output).not.toContainText(retainedQuiet);

  quietPhase = "hang";
  await modal.getByRole("checkbox", { name: "quiet.stdout" }).check();
  await expect(output).toContainText(retainedQuiet, { timeout: 12_000 });
  await expect.poll(() => hangingReselectedQuiet.length, { timeout: 12_000 }).toBeGreaterThan(0);
  await expect(modal.getByRole("alert")).toHaveCount(0);
  await expect(output).toContainText(advancedActive);

  heldQuietFailures.shift()?.();
  const refreshWarning = modal.getByRole("alert");
  await expect(refreshWarning).toContainText(outageStatus === 0 ? /unable to load log stream/i : outageMessage, {
    timeout: 12_000,
  });
  await expect(output).toContainText(retainedQuiet);
  await expect(output).toContainText(advancedActive);
  await expect(output).not.toContainText("Unable to load log stream");
  await expect(modal.locator("[data-awf-stale='true']")).toBeVisible();
  await expect(modal.getByTitle("Showing the last snapshot — live data may be stale")).toBeVisible();

  while (hangingReselectedQuiet.length > 0) {
    hangingReselectedQuiet.shift()?.();
  }
});
}

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

async function mockAwfApi(page: Page, options: MockAwfApiOptions = {}) {
  const { advanceActiveTailAfterFirstRead, quietTailBytes, streamNoiseBytes, streamResponseDelayMs } = options;
  const state: { activeTailPoll: number | null; activeTailReads: number; streamPolls: number; activeMetadataAdvanced?: boolean } = {
    activeTailPoll: null,
    activeTailReads: 0,
    streamPolls: 0,
  };
  await page.route("**/api/awf/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/awf/health") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/awf/console/capabilities") {
      // Canonical local contract includes workspace_logs / workspace_stream so
      // the fail-closed fullscreen log gate allows these positive log cases.
      await fulfillJson(route, localCapabilities());
      return;
    }
    if (path === "/api/awf/console/dashboard-summary") {
      await fulfillJson(route, {
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0,
          executing: 0,
          monitoring_pr: 0,
          awaiting_operator: 0,
          awaiting_human: 0,
          retrying: 0,
          queued: 0,
          completed_last_window: 0,
          cancelled_last_window: 0,
          failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      });
      return;
    }
    if (path === "/api/awf/workspaces/overview") {
      await fulfillJson(route, listEnvelope([workspaceOverview()]));
      return;
    }
    if (path === "/api/awf/metrics/resources/saturation") {
      await fulfillJson(route, resourceSaturation());
      return;
    }
    if (path === "/api/awf/metrics/workspaces/summary") {
      await fulfillJson(route, workspaceReliability());
      return;
    }
    if (path === "/api/awf/merge-queue") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/metrics/failures/summary") {
      await fulfillJson(route, { total_failures: 0, window_hours: 24, taxonomy: [], latest_examples: [] });
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/runtime") {
      await fulfillJson(route, { stack_state: "running", services: [], app_endpoints: [] });
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/events") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/operations") {
      await fulfillJson(route, listEnvelope([]));
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/logs") {
      state.streamPolls += 1;
      const activeMetadataAdvanced = state.activeMetadataAdvanced ??
        (advanceActiveTailAfterFirstRead ? state.activeTailReads > 0 : undefined);
      await fulfillJson(route, listEnvelope(logStreams(state.streamPolls, activeMetadataAdvanced)));
      return;
    }
    if (path.endsWith("/logs/active.stdout")) {
      let poll = state.activeTailPoll ?? state.streamPolls;
      if (advanceActiveTailAfterFirstRead) {
        poll = state.activeTailReads > 0 ? 2 : 1;
      }
      state.activeTailReads += 1;
      await fulfillJson(route, logRead("active.stdout", activeLogData(poll)));
      return;
    }
    if (path.endsWith("/logs/quiet.stdout")) {
      await fulfillJson(route, logRead("quiet.stdout", quietLogData(state.streamPolls, quietTailBytes)));
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs") {
      await fulfillJson(route, workspaceOverview());
      return;
    }
    if (path === "/api/awf/workspaces/ws_logs/stream") {
      const frames: AwfStreamFrame[] = [
        { type: "connected", workspace_id: "ws_logs" },
      ];
      if (typeof streamNoiseBytes === "number") {
        frames.push({
          type: "log",
          seq: 0,
          workspace_id: "ws_logs",
          stream_id: quietStreamId,
          source: "monitor",
          fd: "stdout",
          offset: 0,
          next_offset: streamNoiseBytes,
          data: quietLogData(0, streamNoiseBytes),
        });
      }
      const body = frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");
      if (typeof streamResponseDelayMs === "number" && streamResponseDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, streamResponseDelayMs));
      }
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body,
      });
      return;
    }

    await fulfillJson(route, { detail: { message: `unmocked ${path}` } }, 404);
  });
  return state;
}

function listEnvelope<T>(items: T[]) {
  return { items, next_cursor: null, has_more: false };
}

function workspaceOverviewFor(workspaceId: string) {
  return {
    id: workspaceId,
    workspace_id: workspaceId,
    task_id: `task-${workspaceId}`,
    title: "Log Viewer Workspace",
    task_prompt: "Test prompt",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: `branch/${workspaceId}`,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "xhigh",
    agent_model_source: "mock",
    agent_effort_source: "mock",
    status: "running",
    current_phase: "agent",
    active_operation: null,
    created_at: now,
    updated_at: now,
    lifecycle: [],
    llm_usage: { status: "unavailable" },
    recovery: null,
    coordination_warnings: [],
  };
}

function workspaceOverview() {
  return workspaceOverviewFor("ws_logs");
}

function logStreams(poll: number, activeMetadataAdvanced = poll > 1) {
  const activeLineCount = activeMetadataAdvanced ? 140 : 120;
  return [
    logStream("quiet.stdout", 2_400, 120, quietOpenedAt),
    logStream("active.stdout", activeLineCount * 24, activeLineCount, activeOpenedAt),
  ];
}

function logStream(streamId: string, byteCount: number, lineCount: number, openedAt: string) {
  return {
    stream_id: streamId,
    source: streamId.startsWith("active") ? "recovery" : "monitor",
    name: streamId,
    kind: "stdout",
    path: `/tmp/${streamId}`,
    byte_count: byteCount,
    line_count: lineCount,
    opened_at: openedAt,
    closed_at: null,
  };
}

function logRead(streamId: string, data: string) {
  return {
    stream_id: streamId,
    offset: 0,
    next_offset: data.length,
    eof: true,
    data,
  };
}

function quietLogData(_poll: number, forcedBytes?: number) {
  if (typeof forcedBytes === "number") {
    const prefix = "quiet line 000";
    if (forcedBytes <= prefix.length) {
      return prefix;
    }
    return `${prefix}${"x".repeat(forcedBytes - prefix.length)}`;
  }
  return Array.from({ length: 120 }, (_, index) => `quiet line ${index.toString().padStart(3, "0")}`).join("\n");
}

function activeLogData(poll: number) {
  return Array.from({ length: poll > 1 ? 140 : 120 }, (_, index) =>
    `active line ${index.toString().padStart(3, "0")} poll ${poll}`,
  ).join("\n");
}

function resourceSaturation() {
  return {
    generated_at: now,
    workspace_counts: { by_status: {}, active_total: 1 },
    worker: { max_concurrent_provisions: 10, max_concurrent_executions: 10 },
    resource_defaults: { steady_cpu: 1, steady_memory_gb: 2, peak_cpu: 2, peak_memory_gb: 4 },
    reserved_resources: {
      active_workspace_count: 1,
      steady_cpu: 1,
      steady_memory_gb: 2,
      peak_cpu: 2,
      peak_memory_gb: 4,
      disk_mb: 0,
      dind_slots: 0,
    },
    capacity: {},
    concurrency: {},
    disk: { ok: true },
    admission: { ok: true },
  };
}

function workspaceReliability() {
  return {
    window_hours: 24,
    total_completed: 0,
    total_failed: 0,
    total_cancelled: 0,
    reliability_percentage: 100,
  };
}
