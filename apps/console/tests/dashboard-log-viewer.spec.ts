import { expect, type Page, test } from "@playwright/test";

import type { AwfStreamFrame } from "@/lib/types";

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

async function fulfillJson(
  route: Parameters<Parameters<Page["route"]>[1]>[0],
  body: unknown,
  status = 200,
) {
  await route.fulfill({
    status,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function listEnvelope<T>(items: T[]) {
  return { items, next_cursor: null, has_more: false };
}

function workspaceOverview() {
  return {
    id: "ws_logs",
    workspace_id: "ws_logs",
    task_id: "task-ws_logs",
    title: "Log Viewer Workspace",
    task_prompt: "Test prompt",
    repo_url: "https://github.com/example/awf",
    base_branch: "main",
    branch_name: "branch/ws_logs",
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
