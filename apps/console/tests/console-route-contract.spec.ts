import { expect, test, type Page, type Request } from "@playwright/test";

const WORKSPACE_ID = "ws_route_contract";

type Mode = {
  name: string;
  homePath: string;
  apiPrefix: string;
  operatorPrefix: string;
};

const localMode: Mode = {
  name: "local",
  homePath: "/",
  apiPrefix: "/api/awf",
  operatorPrefix: "/api/operator",
};

const hostedMode: Mode = {
  name: "hosted",
  homePath: "/workspaces",
  apiPrefix: "/api/core-console",
  operatorPrefix: "/api/core-console",
};

async function mockConsoleApis(page: Page, mode: Mode) {
  const overview = {
    workspace_id: WORKSPACE_ID,
    title: "Route contract workspace",
    task_prompt: "prove path wiring",
    repo_url: "https://github.com/example/app.git",
    base_branch: "main",
    branch_name: "feature/route-contract",
    task_class: null,
    owned_paths: [],
    agent: "codex",
    status: "running",
    current_phase: "running",
    version: 3,
    active_operation: null,
    last_event: null,
    pr_url: "https://github.com/example/app/pull/1",
    pr_number: 1,
    failure_reason: null,
    failure_message: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    lifecycle: [],
    llm_usage: null,
    recovery: null,
  };

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === `${mode.apiPrefix}/health`) {
      await route.fulfill({ json: { status: "ok" } });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/overview`) {
      await route.fulfill({ json: { items: [overview], has_more: false, next_cursor: null } });
      return;
    }
    if (path === `${mode.apiPrefix}/metrics/resources/saturation`) {
      await route.fulfill({ json: { generated_at: new Date().toISOString() } });
      return;
    }
    if (path === `${mode.apiPrefix}/metrics/workspaces/summary`) {
      await route.fulfill({ json: { active: 1, failed: 0 } });
      return;
    }
    if (path === `${mode.apiPrefix}/merge-queue`) {
      await route.fulfill({ json: { items: [], has_more: false } });
      return;
    }
    if (path === `${mode.apiPrefix}/metrics/failures/summary`) {
      await route.fulfill({
        json: { taxonomy: [], latest_examples: [], total_failures: 0 },
      });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}`) {
      await route.fulfill({ json: overview });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/runtime`) {
      await route.fulfill({ json: { status: "running" } });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/events`) {
      await route.fulfill({ json: { items: [], has_more: false } });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/operations`) {
      await route.fulfill({ json: { items: [], has_more: false } });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/logs`) {
      await route.fulfill({ json: { items: [], has_more: false } });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/artifacts`) {
      await route.fulfill({
        json: {
          items: [
            {
              artifact_id: "art_plan",
              workspace_id: WORKSPACE_ID,
              name: "plan.md",
              relative_path: "plan.md",
              path: "/tmp/plan.md",
              kind: "md",
              size_bytes: 12,
              modified_at: new Date().toISOString(),
            },
          ],
          has_more: false,
          next_cursor: null,
        },
      });
      return;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/artifacts/download`) {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/markdown; charset=utf-8" },
        body: "# plan\n",
      });
      return;
    }
    if (path.startsWith(`${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/stream`)) {
      await route.fulfill({
        status: 200,
        headers: {
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: `data: ${JSON.stringify({ type: "connected", workspace_id: WORKSPACE_ID })}\n\n`,
      });
      return;
    }
    if (path === `${mode.operatorPrefix}/workspaces/${WORKSPACE_ID}/cancel`) {
      await route.fulfill({
        json: {
          workspace_id: WORKSPACE_ID,
          operation_id: "op_cancel_route",
          operation_status: "succeeded",
          status: "cancelled",
          message: "cancelled",
        },
      });
      return;
    }

    await route.continue();
  });
}

function pathOf(request: Request): string {
  return new URL(request.url()).pathname;
}

async function assertRouteContract(page: Page, mode: Mode) {
  const seen = {
    overview: false,
    detail: false,
    stream: false,
    download: false,
    cancel: false,
  };

  page.on("request", (request) => {
    const path = pathOf(request);
    if (path === `${mode.apiPrefix}/workspaces/overview`) {
      seen.overview = true;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}`) {
      seen.detail = true;
    }
    if (path.startsWith(`${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/stream`)) {
      seen.stream = true;
    }
    if (path === `${mode.apiPrefix}/workspaces/${WORKSPACE_ID}/artifacts/download`) {
      seen.download = true;
    }
    if (
      path === `${mode.operatorPrefix}/workspaces/${WORKSPACE_ID}/cancel` &&
      request.method() === "POST"
    ) {
      seen.cancel = true;
    }
  });

  await mockConsoleApis(page, mode);
  await page.goto(`${mode.homePath}?workspaceId=${WORKSPACE_ID}`);

  await expect(page.getByText("Route contract workspace").first()).toBeVisible({
    timeout: 15_000,
  });
  await expect.poll(() => seen.overview, { timeout: 10_000 }).toBe(true);
  await expect.poll(() => seen.detail, { timeout: 10_000 }).toBe(true);
  await expect.poll(() => seen.stream, { timeout: 10_000 }).toBe(true);

  const cancelButton = page.getByRole("button", { name: "Cancel", exact: true });
  await expect(cancelButton).toBeVisible();
  await cancelButton.click();
  const confirmation = page.getByTestId("operator-cancel-confirm");
  await expect(confirmation).toBeVisible();
  await page.getByRole("button", { name: "Confirm cancel", exact: true }).click();
  await expect.poll(() => seen.cancel, { timeout: 10_000 }).toBe(true);

  await page
    .getByTestId(`workspace-card-${WORKSPACE_ID}`)
    .getByRole("button", { name: "Details", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  const artifacts = page.getByTestId("task-artifacts");
  await expect(artifacts).toBeVisible({ timeout: 15_000 });
  await artifacts.getByRole("button", { name: "Plan", exact: true }).click();
  await expect.poll(() => seen.download, { timeout: 10_000 }).toBe(true);

  expect(seen).toEqual({
    overview: true,
    detail: true,
    stream: true,
    download: true,
    cancel: true,
  });
}

test.describe("console route contract (local defaults)", () => {
  test.use({ baseURL: "http://127.0.0.1:3100" });

  test("list/detail, stream, download, and controls hit /api/awf and /api/operator", async ({
    page,
  }) => {
    await assertRouteContract(page, localMode);
  });
});

test.describe("console route contract (hosted /workspaces)", () => {
  test.use({ baseURL: "http://127.0.0.1:3101" });

  test("list/detail, stream, download, and controls stay under /workspaces bases", async ({
    page,
  }) => {
    await assertRouteContract(page, hostedMode);
  });
});
