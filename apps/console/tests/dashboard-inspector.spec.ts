import { test, expect } from "@playwright/test";

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

    await page.route("/api/awf/workspaces/overview*", async (route) => {
      await route.fulfill({ json: { items: [mockWorkspace], has_more: false } });
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

    await page.route("/api/awf/workspaces/ws_mock123/logs", async (route) => {
      await route.fulfill({ json: { items: [], has_more: false } });
    });
  });

  test("Open and close inspector, verify URL persistence, and test no jump", async ({ page }) => {
    // 1. Initial Load
    await page.goto("/");
    
    // Wait for the workspace list to load and select the workspace
    const workspaceRow = page.locator("text=ws_mock123").first();
    await workspaceRow.waitFor({ state: "visible" });
    await workspaceRow.click();

    // Wait for inspector to open
    const inspector = page.locator("h2", { hasText: "Mock Workspace" }).first();
    await inspector.waitFor({ state: "visible" });

    // Verify URL was updated to include workspaceId
    await expect(page).toHaveURL(/workspaceId=ws_mock123/);

    // Measure global pane to check for jumps later
    const capacityPanel = page.locator("text=Resource / Capacity").first();
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

  test("Responsive layout verification", async ({ page }) => {
    await page.goto("/?workspaceId=ws_mock123");
    const inspectorDrawer = page.locator(".fixed.inset-y-0.right-0").first();
    
    // Desktop layout (default is 1280x720)
    await expect(inspectorDrawer).toHaveClass(/sm:w-\[600px\]/);
    await expect(inspectorDrawer).toHaveClass(/xl:w-\[800px\]/);

    // Mobile layout
    await page.setViewportSize({ width: 375, height: 667 });
    
    // In mobile, it should be full width (w-full is on it) and overlay should be visible
    const overlay = page.locator(".fixed.inset-0.z-40").first();
    await expect(overlay).toBeVisible();
    await expect(inspectorDrawer).toHaveClass(/w-full/);
  });
});
