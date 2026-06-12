import { expect, type Page, test } from "@playwright/test";

function baseWorkspace(workspaceId: string, title: string) {
  return {
    workspace_id: workspaceId,
    title,
    repo_url: "https://github.com/test/repo",
    base_branch: "main",
    agent: "test-agent",
    status: "completed",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    task_prompt: "do the thing",
    lifecycle: [],
    llm_usage: null,
    recovery: null,
    coordination_warnings: [],
  };
}

function artifact(workspaceId: string, name: string) {
  return {
    artifact_id: `art_${workspaceId}_${name}`,
    workspace_id: workspaceId,
    name,
    relative_path: name,
    path: `/work/artifacts/${workspaceId}/${name}`,
    kind: name.endsWith(".json") ? "json" : "md",
    size_bytes: 32,
    modified_at: new Date().toISOString(),
  };
}

async function mockDashboard(page: Page) {
  await page.route("/api/awf/health", async (route) => {
    await route.fulfill({ json: { status: "ok" } });
  });
  await page.route("/api/awf/metrics/resources/saturation", async (route) => {
    await route.fulfill({ json: { generated_at: new Date().toISOString() } });
  });
  await page.route("/api/awf/metrics/workspaces/summary", async (route) => {
    await route.fulfill({ json: { active: 0, failed: 0 } });
  });
  await page.route("/api/awf/merge-queue*", async (route) => {
    await route.fulfill({ json: { items: [], has_more: false } });
  });
  await page.route("/api/awf/metrics/failures/summary", async (route) => {
    await route.fulfill({ json: { taxonomy: [], latest_examples: [], total_failures: 0 } });
  });

  const full = baseWorkspace("ws_full", "Full Artifacts");
  const planOnly = baseWorkspace("ws_plan", "Plan Only");
  const none = baseWorkspace("ws_none", "No Artifacts");
  const failed = baseWorkspace("ws_fail", "List Error");

  await page.route("/api/awf/workspaces/overview*", async (route) => {
    await route.fulfill({ json: { items: [full, planOnly, none, failed], has_more: false } });
  });

  // Plan + conformance present.
  await page.route("/api/awf/workspaces/ws_full/artifacts", async (route) => {
    await route.fulfill({
      json: {
        items: [artifact("ws_full", "plan.md"), artifact("ws_full", "conformance.json")],
        next_cursor: null,
        has_more: false,
      },
    });
  });
  await page.route("/api/awf/workspaces/ws_full/artifacts/download*", async (route) => {
    const url = route.request().url();
    if (url.includes("path=plan.md")) {
      await route.fulfill({
        headers: { "content-type": "text/markdown" },
        body: "# Surfaced Plan\n\n- first deposited step\n",
      });
      return;
    }
    await route.fulfill({
      headers: { "content-type": "application/json" },
      body: '{"status":"satisfied","gaps":[]}',
    });
  });

  // Only the plan artifact present.
  await page.route("/api/awf/workspaces/ws_plan/artifacts", async (route) => {
    await route.fulfill({
      json: { items: [artifact("ws_plan", "plan.md")], next_cursor: null, has_more: false },
    });
  });
  await page.route("/api/awf/workspaces/ws_plan/artifacts/download*", async (route) => {
    await route.fulfill({
      headers: { "content-type": "text/markdown" },
      body: "# Plan Only Body\n",
    });
  });

  // No artifacts deposited.
  await page.route("/api/awf/workspaces/ws_none/artifacts", async (route) => {
    await route.fulfill({ json: { items: [], next_cursor: null, has_more: false } });
  });

  // Artifact list request fails — the error must be surfaced, not swallowed.
  await page.route("/api/awf/workspaces/ws_fail/artifacts", async (route) => {
    await route.fulfill({ status: 500, json: { detail: { message: "Unable to load artifacts." } } });
  });
}

async function openDetails(page: Page, workspaceId: string) {
  await page
    .getByTestId(`workspace-card-${workspaceId}`)
    .getByRole("button", { name: "Details", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
}

test("renders Plan and Validation buttons and their content when both artifacts exist", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_full");
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();

  const planButton = section.getByRole("button", { name: "Plan" });
  const validationButton = section.getByRole("button", { name: "Validation" });
  await expect(planButton).toBeVisible();
  await expect(validationButton).toBeVisible();

  // No content region until a button is clicked.
  await expect(page.getByTestId("task-artifact-content")).toHaveCount(0);

  await planButton.click();
  const content = page.getByTestId("task-artifact-content");
  await expect(content).toContainText("Surfaced Plan");
  await expect(content).toContainText("first deposited step");

  await validationButton.click();
  // Pretty-printed JSON keeps the key/value spacing.
  await expect(content).toContainText('"status": "satisfied"');
});

test("ignores a slow earlier download after switching artifacts", async ({ page }) => {
  await mockDashboard(page);
  // Make the plan download resolve slowly so a subsequent validation click wins
  // the race; the stale plan response must not overwrite the validation content.
  await page.unroute("/api/awf/workspaces/ws_full/artifacts/download*");
  await page.route("/api/awf/workspaces/ws_full/artifacts/download*", async (route) => {
    const url = route.request().url();
    if (url.includes("path=plan.md")) {
      await new Promise((resolve) => setTimeout(resolve, 750));
      await route.fulfill({
        headers: { "content-type": "text/markdown" },
        body: "# Surfaced Plan\n\n- first deposited step\n",
      });
      return;
    }
    await route.fulfill({
      headers: { "content-type": "application/json" },
      body: '{"status":"satisfied","gaps":[]}',
    });
  });

  await page.goto("/");
  await openDetails(page, "ws_full");
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();

  // Click Plan (slow), then immediately switch to Validation (fast).
  await section.getByRole("button", { name: "Plan" }).click();
  await section.getByRole("button", { name: "Validation" }).click();

  const content = page.getByTestId("task-artifact-content");
  await expect(content).toContainText('"status": "satisfied"');

  // Wait past the slow plan download; the stale response must be discarded.
  await page.waitForTimeout(1000);
  await expect(content).toContainText('"status": "satisfied"');
  await expect(content).not.toContainText("Surfaced Plan");
});

test("shows only the Plan button when conformance.json is absent", async ({ page }) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_plan");
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();
  await expect(section.getByRole("button", { name: "Plan" })).toBeVisible();
  await expect(section.getByRole("button", { name: "Validation" })).toHaveCount(0);
});

test("hides the artifacts section when no named artifacts were deposited", async ({ page }) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_none");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByTestId("task-artifacts")).toHaveCount(0);
});

test("surfaces an error banner when the artifact list request fails", async ({ page }) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_fail");
  await expect(page.getByRole("dialog")).toBeVisible();
  // The list fetch failed, so no named artifacts resolve — but the section must
  // still mount to surface the failure instead of looking like an empty result.
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();
  await expect(section).toContainText("Unable to load artifacts.");
  await expect(section.getByRole("button", { name: "Plan" })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Validation" })).toHaveCount(0);
});
