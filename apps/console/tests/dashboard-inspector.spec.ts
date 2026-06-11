import { test, expect, type Page } from "@playwright/test";

async function clickWorkspaceTitle(page: Page, workspaceId: string) {
  const workspaceTitle = page.getByTestId(`workspace-title-${workspaceId}`);
  await workspaceTitle.waitFor({ state: "visible" });
  const titleBox = await workspaceTitle.boundingBox();
  if (!titleBox) {
    throw new Error(`Workspace title ${workspaceId} did not produce a clickable box`);
  }
  await page.mouse.click(titleBox.x + titleBox.width / 2, titleBox.y + titleBox.height / 2);
}

// Since we don't have a real API running in the CI for this test, we need to mock it.
test.describe("Dashboard Workspace Inspector", () => {
  test.beforeEach(async ({ page }) => {
    // Mock the API responses needed for the dashboard to render and show a workspace
    await page.route("/api/awf/health", async (route) => {
      await route.fulfill({ json: { status: "ok" } });
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
