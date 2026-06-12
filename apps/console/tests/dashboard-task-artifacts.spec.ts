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
  const planDownloadFail = baseWorkspace("ws_dlfail", "Plan Download Error");
  const paged = baseWorkspace("ws_paged", "Paged Artifacts");

  // A still-running task whose artifacts are deposited only after the modal is
  // already open. The overview poll bumps updated_at on its second response, and
  // the artifact list flips from empty to populated, so the section must refetch
  // and surface the buttons without the modal being closed and reopened.
  const live = baseWorkspace("ws_live", "Live Monitoring");
  let overviewCalls = 0;

  await page.route("/api/awf/workspaces/overview*", async (route) => {
    overviewCalls += 1;
    // Each poll reports a fresh updated_at, mirroring a workspace that is still
    // advancing. That changing marker is what must re-drive the artifact fetch.
    const liveSnapshot = {
      ...live,
      status: "running",
      updated_at: `2026-06-12T16:38:${String(overviewCalls).padStart(2, "0")}.000Z`,
    };
    await route.fulfill({
      json: {
        items: [full, planOnly, none, failed, planDownloadFail, paged, liveSnapshot],
        has_more: false,
      },
    });
  });

  // The deposit becomes visible only after the overview has polled again — i.e.
  // strictly after the modal first mounted. Gating on the overview-poll count
  // (not the artifact-call count) keeps any mount-time fetches empty even under
  // React StrictMode's double-invoke, so the buttons can appear only once a
  // post-mount updated_at change re-drives the list fetch.
  await page.route("/api/awf/workspaces/ws_live/artifacts*", async (route) => {
    const items =
      overviewCalls >= 2
        ? [artifact("ws_live", "plan.md"), artifact("ws_live", "conformance.json")]
        : [];
    await route.fulfill({ json: { items, next_cursor: null, has_more: false } });
  });
  await page.route("/api/awf/workspaces/ws_live/artifacts/download*", async (route) => {
    await route.fulfill({
      headers: { "content-type": "text/markdown" },
      body: "# Live Plan Body\n",
    });
  });

  // Plan + conformance present.
  await page.route("/api/awf/workspaces/ws_full/artifacts*", async (route) => {
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
  await page.route("/api/awf/workspaces/ws_plan/artifacts*", async (route) => {
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
  await page.route("/api/awf/workspaces/ws_none/artifacts*", async (route) => {
    await route.fulfill({ json: { items: [], next_cursor: null, has_more: false } });
  });

  // Artifact list request fails — the error must be surfaced, not swallowed.
  await page.route("/api/awf/workspaces/ws_fail/artifacts*", async (route) => {
    await route.fulfill({ status: 500, json: { detail: { message: "Unable to load artifacts." } } });
  });

  // Plan artifact is listed, but its download fails — the error banner must show
  // without the empty-state "No prompt stored" text leaking through.
  await page.route("/api/awf/workspaces/ws_dlfail/artifacts*", async (route) => {
    await route.fulfill({
      json: { items: [artifact("ws_dlfail", "plan.md")], next_cursor: null, has_more: false },
    });
  });
  await page.route("/api/awf/workspaces/ws_dlfail/artifacts/download*", async (route) => {
    await route.fulfill({ status: 500, json: { detail: { message: "boom" } } });
  });

  // The named artifacts sort after a full page of unrelated files, so they land
  // on the second page. The section must follow next_cursor before deciding
  // presence — reading only the first page would hide the controls even though
  // plan.md / conformance.json exist and are downloadable.
  await page.route("/api/awf/workspaces/ws_paged/artifacts*", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    if (cursor === null) {
      await route.fulfill({
        json: {
          items: [artifact("ws_paged", "00-early.txt"), artifact("ws_paged", "01-early.txt")],
          next_cursor: "page-2",
          has_more: true,
        },
      });
      return;
    }
    await route.fulfill({
      json: {
        items: [artifact("ws_paged", "plan.md"), artifact("ws_paged", "conformance.json")],
        next_cursor: null,
        has_more: false,
      },
    });
  });
  await page.route("/api/awf/workspaces/ws_paged/artifacts/download*", async (route) => {
    const url = route.request().url();
    if (url.includes("path=plan.md")) {
      await route.fulfill({
        headers: { "content-type": "text/markdown" },
        body: "# Paged Plan\n",
      });
      return;
    }
    await route.fulfill({
      headers: { "content-type": "application/json" },
      body: '{"status":"satisfied","gaps":[]}',
    });
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

test("refetches artifacts as an open, still-running task deposits them", async ({ page }) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_live");

  // The very first artifact-list fetch (modal just opened, task still running)
  // returns nothing, so the buttons can only appear if a later overview poll
  // re-drives the fetch. Asserting they show — without closing/reopening the
  // modal — proves the refetch happened; a single mount-time fetch never would.
  const section = page.getByTestId("task-artifacts");
  await expect(section.getByRole("button", { name: "Plan" })).toBeVisible({ timeout: 15_000 });
  await expect(section.getByRole("button", { name: "Validation" })).toBeVisible();
});

test("follows pagination so controls show when named artifacts are on a later page", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_paged");
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();

  // plan.md / conformance.json only appear on the second page; the buttons can
  // surface only if the section followed next_cursor past the first page.
  await expect(section.getByRole("button", { name: "Plan" })).toBeVisible();
  await expect(section.getByRole("button", { name: "Validation" })).toBeVisible();
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

test("shows the download error without leaking the empty-prompt placeholder", async ({ page }) => {
  await mockDashboard(page);
  await page.goto("/");

  await openDetails(page, "ws_dlfail");
  const section = page.getByTestId("task-artifacts");
  await expect(section).toBeVisible();

  await section.getByRole("button", { name: "Plan" }).click();

  // The download failure surfaces as an error banner...
  await expect(section).toContainText("Request failed with HTTP 500.");
  // ...and the content panel must not render the misleading empty-state path,
  // which would otherwise claim "No prompt stored for this workspace."
  await expect(page.getByTestId("task-artifact-content")).toHaveCount(0);
  await expect(section).not.toContainText("No prompt stored for this workspace.");
});
