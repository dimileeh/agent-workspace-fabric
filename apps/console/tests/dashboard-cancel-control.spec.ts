import { test, expect, type Page } from "@playwright/test";

const WORKSPACE_ID = "ws_mock123";

async function mockBootstrap(page: Page) {
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
    workspace_id: WORKSPACE_ID,
    title: "Mock Workspace",
    repo_url: "https://github.com/test/repo",
    base_branch: "main",
    agent: "test-agent",
    status: "running",
    version: 7,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  await page.route("/api/awf/workspaces/overview*", async (route) => {
    await route.fulfill({ json: { items: [mockWorkspace], has_more: false } });
  });
  await page.route(`/api/awf/workspaces/${WORKSPACE_ID}`, async (route) => {
    await route.fulfill({ json: mockWorkspace });
  });
  await page.route(`/api/awf/workspaces/${WORKSPACE_ID}/runtime`, async (route) => {
    await route.fulfill({ json: { status: "running" } });
  });
  await page.route(`/api/awf/workspaces/${WORKSPACE_ID}/events*`, async (route) => {
    await route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route(`/api/awf/workspaces/${WORKSPACE_ID}/operations*`, async (route) => {
    await route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route(`/api/awf/workspaces/${WORKSPACE_ID}/logs`, async (route) => {
    await route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route("/api/awf/workspaces/*/stream*", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache" },
      body: `data: ${JSON.stringify({ type: "connected", workspace_id: WORKSPACE_ID })}\n\n`,
    });
  });
}

test.describe("Operator cancel control", () => {
  test.beforeEach(async ({ page }) => {
    await mockBootstrap(page);
  });

  test("renders cancel, requires confirmation, and POSTs only on confirm", async ({ page }) => {
    let cancelPosts = 0;
    await page.route(`/api/operator/workspaces/${WORKSPACE_ID}/cancel`, async (route) => {
      cancelPosts += 1;
      await route.fulfill({
        json: {
          workspace_id: WORKSPACE_ID,
          operation_id: "op_cancel123",
          operation_status: "succeeded",
          status: "cancelled",
          message: "cancelled",
        },
      });
    });

    await page.goto(`/?workspaceId=${WORKSPACE_ID}`);

    const cancelButton = page.getByRole("button", { name: "Cancel", exact: true });
    await expect(cancelButton).toBeVisible();
    await expect(cancelButton).toBeEnabled();

    // 1. Clicking Cancel reveals confirmation that mentions the workspace id.
    await cancelButton.click();
    const confirmation = page.getByTestId("operator-cancel-confirm");
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toContainText(WORKSPACE_ID);

    // While the confirmation is open every operator control is frozen so a stray
    // click cannot trigger another action (or re-arm cancel) mid-confirmation.
    await expect(cancelButton).toBeDisabled();

    // 2. Dismiss does not POST.
    await page.getByRole("button", { name: "Dismiss", exact: true }).click();
    await expect(confirmation).not.toBeVisible();
    await page.waitForTimeout(200);
    expect(cancelPosts).toBe(0);

    // 3. Confirm issues exactly one POST and renders the success state.
    await cancelButton.click();
    await expect(confirmation).toBeVisible();
    const [request] = await Promise.all([
      page.waitForRequest(
        (req) =>
          req.url().includes(`/api/operator/workspaces/${WORKSPACE_ID}/cancel`) &&
          req.method() === "POST",
      ),
      page.getByRole("button", { name: "Confirm cancel", exact: true }).click(),
    ]);
    expect(request.method()).toBe("POST");
    await expect(page.getByText("Cancel succeeded:")).toBeVisible();
    expect(cancelPosts).toBe(1);
  });

  test("keeps cancel enabled while a workspace operation is active", async ({ page }) => {
    let cancelPosts = 0;
    await page.route(`/api/operator/workspaces/${WORKSPACE_ID}/cancel`, async (route) => {
      cancelPosts += 1;
      await route.fulfill({
        json: {
          workspace_id: WORKSPACE_ID,
          operation_id: "op_cancel123",
          operation_status: "succeeded",
          status: "cancelled",
          message: "cancelled",
        },
      });
    });

    const workspacePayload = () => ({
      workspace_id: WORKSPACE_ID,
      title: "Mock Workspace",
      repo_url: "https://github.com/test/repo",
      base_branch: "main",
      agent: "test-agent",
      status: "running",
      active_operation: "op_background",
      version: 7,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    });

    // Re-register the overview/detail routes so they reflect the mutable
    // active-operation state instead of the static bootstrap payload.
    await page.route("/api/awf/workspaces/overview*", async (route) => {
      await route.fulfill({ json: { items: [workspacePayload()], has_more: false } });
    });
    await page.route(`/api/awf/workspaces/${WORKSPACE_ID}`, async (route) => {
      await route.fulfill({ json: workspacePayload() });
    });

    await page.goto(`/?workspaceId=${WORKSPACE_ID}`);

    const cancelButton = page.getByRole("button", { name: "Cancel", exact: true });
    await expect(cancelButton).toBeVisible();
    await expect(cancelButton).toBeEnabled();
    await expect(cancelButton.locator("..")).not.toContainText("active operation");
    await expect(page.getByText("active operation", { exact: true })).toHaveCount(0);

    // A background AWF operation must not disable the emergency cancel path.
    await cancelButton.click();
    const confirmation = page.getByTestId("operator-cancel-confirm");
    await expect(confirmation).toBeVisible();

    await page.getByRole("button", { name: "Confirm cancel", exact: true }).click();
    await expect(page.getByText("Cancel succeeded:")).toBeVisible();
    expect(cancelPosts).toBe(1);
  });

  test("does not carry an open cancel confirmation across a workspace switch", async ({ page }) => {
    const SECOND_WORKSPACE_ID = "ws_mock456";

    const workspacePayload = (id: string, title: string) => ({
      workspace_id: id,
      title,
      repo_url: "https://github.com/test/repo",
      base_branch: "main",
      agent: "test-agent",
      status: "running",
      version: 7,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      lifecycle: [],
      llm_usage: null,
      recovery: null,
    });

    // Two running, cancel-enabled workspaces. Switching between them keeps cancel
    // enabled, so only a per-workspace state reset can drop a stale confirmation.
    await page.route("/api/awf/workspaces/overview*", async (route) => {
      await route.fulfill({
        json: {
          items: [
            workspacePayload(WORKSPACE_ID, "First Workspace"),
            workspacePayload(SECOND_WORKSPACE_ID, "Second Workspace"),
          ],
          has_more: false,
        },
      });
    });
    for (const id of [WORKSPACE_ID, SECOND_WORKSPACE_ID]) {
      const title = id === WORKSPACE_ID ? "First Workspace" : "Second Workspace";
      await page.route(`/api/awf/workspaces/${id}`, async (route) => {
        await route.fulfill({ json: workspacePayload(id, title) });
      });
      await page.route(`/api/awf/workspaces/${id}/runtime`, async (route) => {
        await route.fulfill({ json: { status: "running" } });
      });
      await page.route(`/api/awf/workspaces/${id}/events*`, async (route) => {
        await route.fulfill({ json: { items: [], has_more: false } });
      });
      await page.route(`/api/awf/workspaces/${id}/operations*`, async (route) => {
        await route.fulfill({ json: { items: [], has_more: false } });
      });
      await page.route(`/api/awf/workspaces/${id}/logs`, async (route) => {
        await route.fulfill({ json: { items: [], has_more: false } });
      });
    }

    await page.goto(`/?workspaceId=${WORKSPACE_ID}`);

    const cancelButton = page.getByRole("button", { name: "Cancel", exact: true });
    await expect(cancelButton).toBeVisible();
    await expect(cancelButton).toBeEnabled();

    // Open the cancel confirmation for the first workspace.
    await cancelButton.click();
    const confirmation = page.getByTestId("operator-cancel-confirm");
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toContainText(WORKSPACE_ID);

    // Switch to the second (also cancel-enabled) workspace. The destructive
    // confirmation must not survive the switch onto a different workspace.
    await page
      .getByRole("button", { name: "Open workspace details for Second Workspace" })
      .click();
    await expect(page.getByText(SECOND_WORKSPACE_ID, { exact: false }).first()).toBeVisible();
    await expect(confirmation).not.toBeVisible();
  });
});
