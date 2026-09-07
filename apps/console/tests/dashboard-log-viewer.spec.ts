import { expect, type Page, test } from "@playwright/test";

import type { AwfStreamFrame } from "@/lib/types";

import { fulfillJson, localCapabilities } from "./fixtures/console-api";

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

test("fullscreen logs default to ascending, preserve manual scroll, and order tails by stream activity", async ({
  page,
}) => {
  const api = await mockAwfApi(page);

  await page.goto("/");
  await waitForConsoleReady(page);

  await page.getByTestId("workspace-card-ws_logs").getByRole("button", { name: "Logs", exact: true }).click();

  const modal = page.locator(".fixed.inset-0.z-50");
  await expect(modal.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(modal.getByRole("button", { name: "asc" })).toBeVisible();

  const output = modal.getByTestId("log-output");
  await expect(output).toBeVisible();
  await expect(output).toContainText("active.stdout");

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
  let releaseHeldStream: (() => void) | null = null;
  const heldStream = new Promise<void>((resolve) => {
    releaseHeldStream = resolve;
  });
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
      await heldStream;
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

  releaseHeldStream?.();
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await page.waitForTimeout(2_000);
  await expect(inspector.getByText(liveSecret)).toHaveCount(0);
  await expect(inspector.getByText(authorizedMarker)).toHaveCount(0);
  await expect(output).toContainText("No log data loaded.");
});
}

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

async function mockAwfApi(page: Page, options: MockAwfApiOptions = {}) {
  const { advanceActiveTailAfterFirstRead, quietTailBytes, streamNoiseBytes, streamResponseDelayMs } = options;
  const state: { activeTailPoll: number | null; activeTailReads: number; streamPolls: number } = {
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
      const activeMetadataAdvanced = advanceActiveTailAfterFirstRead ? state.activeTailReads > 0 : undefined;
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
