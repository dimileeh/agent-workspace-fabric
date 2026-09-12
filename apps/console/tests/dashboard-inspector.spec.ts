import { test, expect, type Page } from "@playwright/test";

import { fulfillJson, localCapabilities } from "./fixtures/console-api";

async function clickWorkspaceTitle(page: Page, workspaceId: string) {
  const workspaceTitle = page.getByTestId(`workspace-title-${workspaceId}`);
  await workspaceTitle.waitFor({ state: "visible" });
  const titleBox = await workspaceTitle.boundingBox();
  if (!titleBox) {
    throw new Error(`Workspace title ${workspaceId} did not produce a clickable box`);
  }
  await page.mouse.click(titleBox.x + titleBox.width / 2, titleBox.y + titleBox.height / 2);
}

async function expectInsideViewport(locator: ReturnType<Page["locator"]>, width: number) {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  expect(box, `expected a box at ${width}px`).not.toBeNull();
  expect(box!.x, `left edge at ${width}px`).toBeGreaterThanOrEqual(-1);
  expect(box!.x + box!.width, `right edge at ${width}px`).toBeLessThanOrEqual(width + 1);
}

// Since we don't have a real API running in the CI for this test, we need to mock it.
test.describe("Dashboard Workspace Inspector", () => {
  test.beforeEach(async ({ page }) => {
    // Mock the API responses needed for the dashboard to render and show a workspace
    await page.route("/api/awf/health", async (route) => {
      await route.fulfill({ json: { status: "ok" } });
    });


  await page.route("/api/awf/console/capabilities", async (route) => {
    // workspace_logs / workspace_stream must be advertised or inspector tails stay empty.
    await fulfillJson(route, localCapabilities());
  });
  await page.route("/api/awf/console/dashboard-summary", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        schema_version: 1,
        scope: "local",
        generated_at: "2026-09-06T17:00:00Z",
        as_of: "2026-09-06T17:00:00Z",
        last_success_at: "2026-09-06T17:00:00Z",
        window: { anchor: "generated_at", since_hours: 24, start: "2026-09-05T17:00:00Z" },
        coverage: { status: "complete", notes: [] },
        counts: {
          active: 0, executing: 0, monitoring_pr: 0, awaiting_operator: 0, awaiting_human: 0, retrying: 0, queued: 0,
          completed_last_window: 0, cancelled_last_window: 0, failed_last_window: 0,
        },
        overlap: {
          awaiting_human_subset_of_monitoring_pr: true,
          awaiting_operator_in_active_not_executing: true,
          retrying_in_active_not_executing: true,
        },
      }),
    });
  });

  await page.route("/api/awf/metrics/resources/saturation", async (route) => {
      await route.fulfill({ json: { generated_at: new Date().toISOString() } });
    });

    await page.route("/api/awf/metrics/workspaces/summary", async (route) => {
      await route.fulfill({ json: { active: 1, failed: 0 } });
    });

    await page.route("/api/awf/merge-queue*", async (route) => {
      await route.fulfill({ json: { items: [], has_more: false } });
    });

    await page.route("/api/awf/metrics/failures/summary", async (route) => {
      await route.fulfill({ json: { taxonomy: [], latest_examples: [], total_failures: 0 } });
    });

    const mockWorkspace = {
      workspace_id: "ws_mock123",
      title: "Mock Workspace",
      repo_url: "https://github.com/test/repo",
      base_branch: "main",
      agent: "test-agent",
      status: "running",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    };
    const otherWorkspace = {
      ...mockWorkspace,
      workspace_id: "ws_other456",
      title: "Other Workspace",
      status: "completed",
    };

    await page.route("/api/awf/workspaces/overview*", async (route) => {
      await route.fulfill({ json: { items: [mockWorkspace, otherWorkspace], has_more: false } });
    });

    await page.route("/api/awf/workspaces/ws_mock123", async (route) => {
      await route.fulfill({ json: mockWorkspace });
    });

    await page.route("/api/awf/workspaces/ws_mock123/runtime", async (route) => {
      await route.fulfill({ json: { status: "running" } });
    });

    await page.route("/api/awf/workspaces/ws_mock123/events*", async (route) => {
      await route.fulfill({ json: { items: [], has_more: false } });
    });

    await page.route("/api/awf/workspaces/ws_mock123/operations*", async (route) => {
      await route.fulfill({ json: { items: [], has_more: false } });
    });

    await page.route("/api/awf/workspaces/ws_other456/logs", async (route) => {
      await route.fulfill({ json: { items: [workspaceLogStream("ws_other456")], has_more: false } });
    });

    await page.route("/api/awf/workspaces/ws_mock123/logs", async (route) => {
      await route.fulfill({ json: { items: [workspaceLogStream("ws_mock123")], has_more: false } });
    });

    await page.route("/api/awf/workspaces/*/logs/agent.stdout*", async (route) => {
      const workspaceId = route.request().url().includes("ws_other456") ? "ws_other456" : "ws_mock123";
      await route.fulfill({
        json: {
          stream_id: "agent.stdout",
          offset: 0,
          next_offset: 24,
          eof: true,
          data: `${workspaceId} agent output\n`,
        },
      });
    });

    await page.route("/api/awf/workspaces/*/stream*", async (route) => {
      const workspaceId = route.request().url().includes("ws_other456") ? "ws_other456" : "ws_mock123";
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
      });
    });
  });

  test("Open and close inspector, verify URL persistence, and test no jump", async ({ page }) => {
    // 1. Initial Load
    await page.goto("/");

    // Wait for the workspace list to load and select the workspace
    await clickWorkspaceTitle(page, "ws_mock123");

    // Wait for inspector to open
    const inspector = page.locator("h2", { hasText: "Mock Workspace" }).first();
    await inspector.waitFor({ state: "visible" });

    // Verify URL was updated to include workspaceId
    await expect(page).toHaveURL(/workspaceId=ws_mock123/);

    // Measure global pane to check for jumps later
    const capacityPanel = page.locator("text=Resource / Runtime Capacity").first();
    await capacityPanel.waitFor({ state: "visible" });
    const initialBox = await capacityPanel.boundingBox();

    // 2. Close Inspector
    const closeBtn = page.getByRole("button", { name: "Close inspector" });
    await closeBtn.click();

    // Verify inspector is closed (we used hidden for the overlay and translate-x-full)
    // Wait for the class to update
    await expect(page.locator(".fixed.inset-y-0.right-0").first()).toHaveClass(/translate-x-full/);

    // Verify URL updated to remove workspaceId
    await expect(page).not.toHaveURL(/workspaceId/);

    // 3. No Jump Validation
    const afterBox = await capacityPanel.boundingBox();
    expect(initialBox?.width).toBeCloseTo(afterBox!.width, 1);
    expect(initialBox?.height).toBeCloseTo(afterBox!.height, 1);
    expect(initialBox?.x).toBeCloseTo(afterBox!.x, 1);
    expect(initialBox?.y).toBeCloseTo(afterBox!.y, 1);

    // 4. Persistence on Reload
    // Navigate manually to the URL with the ID
    await page.goto("/?workspaceId=ws_mock123");

    // Wait for inspector to be visible (translate-x-0)
    await expect(page.locator(".fixed.inset-y-0.right-0").first()).toHaveClass(/translate-x-0/);
    await expect(page.locator("h2", { hasText: "Mock Workspace" }).first()).toBeVisible();
  });

  test("Workspace id copies to clipboard without opening inspector", async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: async (text: string) => {
            (window as typeof window & { __copiedWorkspaceId?: string }).__copiedWorkspaceId = text;
          },
        },
      });
    });

    await page.goto("/");

    const copyId = page.getByLabel("Copy workspace id ws_mock123");
    await expect(copyId).toBeVisible();
    await copyId.click();

    await expect.poll(() => page.evaluate(() => (window as typeof window & { __copiedWorkspaceId?: string }).__copiedWorkspaceId)).toBe("ws_mock123");
    await expect(page.getByText("copied", { exact: true })).toBeVisible();
    await expect(page).not.toHaveURL(/workspaceId=ws_mock123/);
    await expect(page.locator("h2", { hasText: "Mock Workspace" }).first()).not.toBeVisible();

    await expect(page.getByText("copied", { exact: true })).not.toBeVisible({ timeout: 2500 });
  });

  test("Workspace id copies via execCommand fallback when the Clipboard API is unavailable (HTTP/Tailscale)", async ({ page }) => {
    // Simulate a non-secure context (plain HTTP via a Tailscale address): no async
    // Clipboard API, so the helper must fall back to document.execCommand("copy").
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
      document.execCommand = ((command: string) => {
        if (command === "copy") {
          const active = document.activeElement as HTMLTextAreaElement | null;
          (window as typeof window & { __copiedViaExec?: string }).__copiedViaExec = active?.value;
          return true;
        }
        return false;
      }) as typeof document.execCommand;
    });

    await page.goto("/");

    const copyId = page.getByLabel("Copy workspace id ws_mock123");
    await expect(copyId).toBeVisible();
    await copyId.click();

    await expect
      .poll(() => page.evaluate(() => (window as typeof window & { __copiedViaExec?: string }).__copiedViaExec))
      .toBe("ws_mock123");
    await expect(page.getByText("copied", { exact: true })).toBeVisible();
    await expect(page).not.toHaveURL(/workspaceId=ws_mock123/);
    await expect(page.locator("h2", { hasText: "Mock Workspace" }).first()).not.toBeVisible();
  });

  test("Inspector fullscreen logs always use the currently inspected workspace", async ({ page }) => {
    await page.goto("/");

    await page
      .getByTestId("workspace-card-ws_other456")
      .getByRole("button", { name: "Logs" })
      .click();
    await expect(page.getByRole("heading", { name: "Logs" }).first()).toBeVisible();
    await expect(page.locator(".fixed.inset-0.z-50").getByRole("heading", { name: "Other Workspace" })).toBeVisible();
    await page.locator(".fixed.inset-0.z-50").getByRole("button", { name: "Close" }).click();
    await expect(page.locator(".fixed.inset-0.z-50")).not.toBeVisible();

    await clickWorkspaceTitle(page, "ws_mock123");
    await expect(page.locator("h2", { hasText: "Mock Workspace" }).first()).toBeVisible();
    await expect(page.getByText("ws_mock123 agent output").first()).toBeVisible();

    await page.getByRole("button", { name: "Fullscreen" }).click();

    await expect(page.getByRole("heading", { name: "Logs" }).first()).toBeVisible();
    await expect(page.locator(".fixed.inset-0.z-50").getByRole("heading", { name: "Mock Workspace" })).toBeVisible();
    await expect(page.locator(".fixed.inset-0.z-50").getByRole("heading", { name: "Other Workspace" })).not.toBeVisible();
  });

  test("Responsive layout verification", async ({ page }) => {
    await page.goto("/?workspaceId=ws_mock123");
    const inspectorDrawer = page.locator(".fixed.inset-y-0.right-0").first();

    // Wait for inspector to open before asserting layout
    await expect(inspectorDrawer).toHaveClass(/translate-x-0/);
    await inspectorDrawer.evaluate(async (element) => {
      await Promise.all(element.getAnimations().map((animation) => animation.finished));
    });

    // Desktop layout (default is 1280x720): the inspector fills the main
    // dashboard canvas while preserving the workspace list column.
    await expect(inspectorDrawer).toHaveClass(/xl:w-\[calc\(100vw-440px\)\]/);
    const desktopBox = await inspectorDrawer.boundingBox();
    expect(desktopBox?.x).toBeCloseTo(440, 1);
    expect(desktopBox?.width).toBeCloseTo(840, 1);

    // Awkward desktop widths keep the inspector content in one column. The
    // drawer is broad, but not broad enough for dense Workspace + Timeline
    // panels to sit side by side without colliding.
    await page.setViewportSize({ width: 1463, height: 900 });
    const workspacePanel = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Workspace", exact: true }) })
      .first();
    const timelinePanel = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Timeline", exact: true }) })
      .first();
    const workspaceBox = await workspacePanel.boundingBox();
    const timelineBox = await timelinePanel.boundingBox();
    expect(workspaceBox).not.toBeNull();
    expect(timelineBox).not.toBeNull();
    expect(workspaceBox!.y + workspaceBox!.height).toBeLessThanOrEqual(timelineBox!.y + 1);

    // Wider desktop keeps the wider workspace list visible and gives the
    // inspector the rest of the page.
    await page.setViewportSize({ width: 1600, height: 900 });
    const wideBox = await inspectorDrawer.boundingBox();
    expect(wideBox?.x).toBeCloseTo(500, 1);
    expect(wideBox?.width).toBeCloseTo(1100, 1);

    // Tablet/small desktop layout covers the whole page instead of using a
    // cramped partial drawer.
    await page.setViewportSize({ width: 1024, height: 768 });
    const tabletBox = await inspectorDrawer.boundingBox();
    expect(tabletBox?.x).toBeCloseTo(0, 1);
    expect(tabletBox?.width).toBeCloseTo(1024, 1);

    // Mobile layout
    await page.setViewportSize({ width: 375, height: 667 });

    // In mobile, it should be full width (w-full is on it) and overlay should be visible
    const overlay = page.locator(".fixed.inset-0.z-40").first();
    await expect(overlay).toBeVisible();
    await expect(inspectorDrawer).toHaveClass(/w-full/);
  });

  test("long workspace summary stays inside the inspector at supported widths", async ({ page }) => {
    const workspaceId = "ws_layout_overflow";
    const longToken = "pathological-unbroken-workspace-value-".repeat(18);
    const layoutWorkspace = {
      workspace_id: workspaceId,
      task_id: `task_${workspaceId}`,
      task_key: `AWF-${longToken}`,
      title: `Inspector layout ${longToken}`,
      task_prompt: "Verify the workspace inspector layout.",
      repo_url: `https://github.com/example/${longToken}.git`,
      base_branch: `base/${longToken}`,
      branch_name: `awf/${longToken}`,
      agent: "codex",
      agent_model: `gpt-${longToken}`,
      agent_effort: "high",
      agent_model_source: "task_policy",
      agent_effort_source: "default",
      requested_model: `requested-${longToken}`,
      requested_model_source: "task_policy",
      confirmed_execution_model: `confirmed-${longToken}`,
      confirmed_execution_model_source: "execution_evidence",
      status: "running",
      subphase: null,
      current_phase: "running",
      active_operation: null,
      created_at: "2026-09-11T12:00:00Z",
      updated_at: "2026-09-11T12:05:00Z",
      last_activity_at: "2026-09-11T12:05:00Z",
      lifecycle: [],
      llm_usage: null,
      recovery: null,
      coordination_warnings: [],
      is_stale_running: false,
      pr_url: "https://github.com/example/repo/pull/963",
      pr_number: 963,
    };

    await page.route("/api/awf/workspaces/overview*", async (route) => {
      await fulfillJson(route, { items: [layoutWorkspace], next_cursor: null, has_more: false });
    });
    await page.route(/\/api\/awf\/workspaces\/ws_layout_overflow(?:\/.*)?(?:\?.*)?$/, async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === `/api/awf/workspaces/${workspaceId}`) {
        await fulfillJson(route, { ...layoutWorkspace, id: workspaceId, version: 1 });
      } else if (path.endsWith("/runtime")) {
        await fulfillJson(route, { status: "running" });
      } else if (path.endsWith("/stream")) {
        await route.fulfill({
          status: 200,
          headers: { "content-type": "text/event-stream; charset=utf-8" },
          body: `data: ${JSON.stringify({ type: "connected", workspace_id: workspaceId })}\n\n`,
        });
      } else {
        await fulfillJson(route, { items: [], next_cursor: null, has_more: false });
      }
    });

    await page.goto(`/?workspaceId=${workspaceId}`);
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector).toHaveClass(/translate-x-0/);
    await inspector.evaluate(async (element) => {
      await Promise.all(element.getAnimations().map((animation) => animation.finished));
    });
    const summary = inspector
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Workspace", exact: true }) })
      .first();
    await expect(summary).toBeVisible();

    const viewports = [
      { width: 320, columns: 1 },
      { width: 375, columns: 1 },
      { width: 390, columns: 1 },
      { width: 430, columns: 1 },
      { width: 768, columns: 2 },
      { width: 1280, columns: 4 },
      { width: 1720, columns: 4 },
    ];
    for (const viewport of viewports) {
      await page.setViewportSize({ width: viewport.width, height: 1000 });
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

      const overflowingDescendants = await summary.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        return Array.from(element.querySelectorAll<HTMLElement>("*"))
          .filter((descendant) => {
            const style = getComputedStyle(descendant);
            const box = descendant.getBoundingClientRect();
            return (
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              box.width > 1 &&
              box.height > 1 &&
              (box.left < bounds.left - 1 || box.right > bounds.right + 1)
            );
          })
          .map((descendant) => ({
            tag: descendant.tagName,
            className: descendant.className,
            left: descendant.getBoundingClientRect().left,
            right: descendant.getBoundingClientRect().right,
            summaryLeft: bounds.left,
            summaryRight: bounds.right,
          }));
      });
      expect(overflowingDescendants, `summary descendants at ${viewport.width}px`).toEqual([]);

      const status = summary.getByText("running", { exact: true }).first();
      const pullRequest = summary.getByRole("link", { name: "PR #963", exact: true });
      const reload = inspector.getByRole("button", { name: "Reload workspace", exact: true });
      const close = inspector.getByRole("button", { name: "Close inspector", exact: true });
      for (const reachable of [status, pullRequest, reload, close]) {
        await expectInsideViewport(reachable, viewport.width);
      }

      const factColumns = await Promise.all(
        ["Workspace", "Task key", "Agent", "Requested model"].map(async (label) => {
          const fact = summary.locator(".label-caps", { hasText: label }).first().locator("..");
          const box = await fact.boundingBox();
          expect(box, `${label} fact at ${viewport.width}px`).not.toBeNull();
          return Math.round(box!.x);
        }),
      );
      expect(new Set(factColumns).size, `fact columns at ${viewport.width}px`).toBe(
        viewport.columns,
      );
    }

    await inspector.getByRole("button", { name: "Reload workspace", exact: true }).click();
    await expect(summary.getByText("running", { exact: true }).first()).toBeVisible();
    await inspector.getByRole("button", { name: "Close inspector", exact: true }).click();
    await expect(inspector).toHaveClass(/translate-x-full/);
  });
});

function workspaceLogStream(workspaceId: string) {
  return {
    stream_id: "agent.stdout",
    source: "agent",
    name: `${workspaceId} stdout`,
    kind: "stdout",
    path: `/tmp/${workspaceId}.log`,
    byte_count: 24,
    line_count: 1,
    opened_at: new Date().toISOString(),
    closed_at: null,
  };
}
