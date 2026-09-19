import { expect, test, type Page, type Route } from "@playwright/test";

import type { WorkspaceOverview } from "@/lib/types";

import { fulfillJson, mockAwfConsoleApi } from "./fixtures/console-api";

const HOSTED_ORIGIN = "http://127.0.0.1:3191/workspaces?org_id=org_synthetic&project_id=p_test";

const OVERVIEW_LEAK = "SYNTHETIC_OVERVIEW_PROMPT_MUST_NOT_APPEAR";
const DETAIL_ALPHA = "SYNTHETIC_DETAIL_PROMPT_ALPHA";
const DETAIL_BRAVO = "SYNTHETIC_DETAIL_PROMPT_BRAVO";
const WITHHELD = "Synthetic withheld: the task prompt is hidden from this caller.";
const FOREIGN_PROMPT = "SYNTHETIC_FOREIGN_WORKSPACE_PROMPT";
const LATE_PROMPT = "SYNTHETIC_LATE_DETAIL_PROMPT";
const REJECTED_SECRET = "synthetic-rejected-secret";

function overviewItem(
  workspaceId: string,
  title: string,
  taskPrompt: string,
): WorkspaceOverview {
  return {
    workspace_id: workspaceId,
    task_id: `task_${workspaceId}`,
    title,
    task_prompt: taskPrompt,
    repo_url: "https://github.com/example/awf.git",
    base_branch: "development",
    branch_name: `awf/${workspaceId}`,
    agent: "codex",
    agent_model: "gpt-5.5",
    agent_effort: "high",
    agent_model_source: "task_policy",
    agent_effort_source: "default",
    network_posture: "restricted",
    lifecycle: [],
    llm_usage: {
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cost_estimate: null,
      currency: null,
      status: "unavailable",
      source: "none",
      reason: "usage_not_reported",
    },
    recovery: null,
    coordination_warnings: [],
    provider_readiness_preflight: null,
    status: "cancelled",
    subphase: null,
    last_activity_at: "2026-09-06T12:15:00Z",
    last_log_at: null,
    is_stale_running: false,
    current_phase: "cancelled",
    active_operation: null,
    last_event: null,
    pr_url: null,
    pr_number: null,
    failure_reason: null,
    failure_message: null,
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:45:00Z",
  };
}

function isWorkspaceDetailPath(pathname: string): boolean {
  const match = pathname.match(/\/api\/(?:awf|core-console)\/workspaces\/([^/]+)$/);
  return match !== null && match[1] !== "overview";
}

function detailReads(urls: string[]): string[] {
  return urls.filter((url) => isWorkspaceDetailPath(new URL(url).pathname));
}

async function installDetailRoute(
  page: Page,
  reads: string[],
  handler: (route: Route, id: string, url: URL) => Promise<void>,
) {
  await page.route(
    (url) => isWorkspaceDetailPath(url.pathname),
    async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET" || !isWorkspaceDetailPath(url.pathname)) {
        await route.fallback();
        return;
      }
      reads.push(url.toString());
      const id = decodeURIComponent(url.pathname.split("/").pop() ?? "");
      await handler(route, id, url);
    },
  );
}

async function openConsole(page: Page, hosted: boolean) {
  await page.goto(hosted ? HOSTED_ORIGIN : "/");
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

async function openDetails(page: Page, workspaceId: string) {
  await page
    .getByTestId(`workspace-card-${workspaceId}`)
    .getByRole("button", { name: "Details", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: /Task details/i });
  await expect(dialog).toBeVisible();
  return dialog;
}

test("hosted empty overview shows the authorized detail prompt", async ({ page }) => {
  const reads: string[] = [];
  const primary = overviewItem("ws_prompt_alpha", "Alpha cancelled task", "");
  const secondary = overviewItem("ws_prompt_bravo", "Bravo cancelled task", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [primary, secondary] });
  await installDetailRoute(page, reads, async (route, id) => {
    await fulfillJson(route, {
      id,
      task_prompt: id === primary.workspace_id ? DETAIL_ALPHA : DETAIL_BRAVO,
    });
  });

  await openConsole(page, true);
  await expect(page.getByTestId(`workspace-card-${primary.workspace_id}`)).toBeVisible();
  await expect(page.getByTestId(`workspace-card-${secondary.workspace_id}`)).toBeVisible();
  expect(detailReads(reads)).toEqual([]);

  const dialog = await openDetails(page, primary.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText("No prompt stored for this workspace.");
  await expect(prompt).not.toContainText(DETAIL_BRAVO);

  const readsAfterOpen = detailReads(reads);
  expect(readsAfterOpen).toHaveLength(1);
  const detailUrl = readsAfterOpen[0] ?? "";
  expect(detailUrl).toContain(`/api/core-console/workspaces/${primary.workspace_id}`);
  expect(detailUrl).toContain("org_id=org_synthetic");
  expect(detailUrl).toContain("project_id=p_test");
  expect(detailReads(reads).filter((url) => url.includes(secondary.workspace_id))).toEqual([]);
});

test("local populated overview still shows the detail prompt", async ({ page }) => {
  const reads: string[] = [];
  const primary = overviewItem("ws_prompt_local", "Local populated task", DETAIL_ALPHA);
  const secondary = overviewItem("ws_prompt_local_other", "Local other task", DETAIL_BRAVO);
  await mockAwfConsoleApi(page, { mode: "local", overviewItems: [primary, secondary] });
  await installDetailRoute(page, reads, async (route, id) => {
    await fulfillJson(route, { id, task_prompt: id === primary.workspace_id ? DETAIL_ALPHA : DETAIL_BRAVO });
  });

  await openConsole(page, false);
  await expect(page.getByTestId(`workspace-card-${secondary.workspace_id}`)).toBeVisible();
  expect(detailReads(reads)).toEqual([]);

  const dialog = await openDetails(page, primary.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(DETAIL_ALPHA);
  expect(detailReads(reads)).toHaveLength(1);
  const detailUrl = new URL(detailReads(reads)[0] ?? "");
  expect(detailUrl.pathname).toBe(`/api/awf/workspaces/${primary.workspace_id}`);
  expect(detailUrl.searchParams.has("org_id")).toBe(false);
  expect(detailUrl.searchParams.has("project_id")).toBe(false);
});

test("modal B shows B while inspector A stays selected", async ({ page }) => {
  const reads: string[] = [];
  const alpha = overviewItem("ws_prompt_inspector_a", "Inspector alpha", "");
  const bravo = overviewItem("ws_prompt_inspector_b", "Inspector bravo", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [alpha, bravo] });
  await installDetailRoute(page, reads, async (route, id) => {
    await fulfillJson(route, {
      id,
      task_prompt: id === alpha.workspace_id ? DETAIL_ALPHA : DETAIL_BRAVO,
    });
  });

  await openConsole(page, true);
  await page.getByRole("button", { name: `Open workspace details for ${alpha.title}`, exact: true }).click();
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  const inspectorTitle = inspector.getByRole("heading").first();
  await expect(inspectorTitle).toHaveText(alpha.title);
  const beforeModal = detailReads(reads).length;

  const dialog = await openDetails(page, bravo.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(DETAIL_BRAVO);
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(inspectorTitle).toHaveText(alpha.title);
  await expect(dialog.getByRole("heading", { name: bravo.title })).toBeVisible();

  const modalReads = detailReads(reads).slice(beforeModal);
  expect(modalReads.length).toBeGreaterThan(0);
  expect(modalReads.every((url) => url.includes(`/workspaces/${bravo.workspace_id}`))).toBe(true);
});

test("genuinely absent detail prompt is shown only after loading", async ({ page }) => {
  const reads: string[] = [];
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const empty = overviewItem("ws_prompt_empty", "Empty prompt task", "");
  const blank = overviewItem("ws_prompt_blank", "Blank prompt task", "   ");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [empty, blank] });
  await installDetailRoute(page, reads, async (route, id) => {
    if (id === empty.workspace_id) {
      await gate;
      await fulfillJson(route, { id, task_prompt: "" });
      return;
    }
    await fulfillJson(route, { id, task_prompt: " \n\t " });
  });

  await openConsole(page, true);
  const dialog = await openDetails(page, empty.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt.getByRole("status")).toContainText("Loading task prompt");
  await expect(prompt).not.toContainText("No prompt stored for this workspace.");
  release?.();
  await expect(prompt).toContainText("No prompt stored for this workspace.");
  await expect(prompt.getByRole("status")).toHaveCount(0);

  await dialog.getByRole("button", { name: "Close task details" }).click();
  const blankDialog = await openDetails(page, blank.workspace_id);
  await expect(blankDialog.getByTestId("task-details-prompt")).toContainText(
    "No prompt stored for this workspace.",
  );
});

test("withheld detail text is shown as the prompt", async ({ page }) => {
  const reads: string[] = [];
  const item = overviewItem("ws_prompt_withheld", "Withheld prompt task", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [item, overviewItem("ws_prompt_withheld_b", "Other", "")] });
  await installDetailRoute(page, reads, async (route, id) => {
    await fulfillJson(route, { id, task_prompt: WITHHELD });
  });

  await openConsole(page, true);
  expect(detailReads(reads)).toEqual([]);
  const dialog = await openDetails(page, item.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(WITHHELD);
  await expect(prompt).not.toContainText("No prompt stored for this workspace.");
  await expect(prompt.getByRole("alert")).toHaveCount(0);
  expect(detailReads(reads)).toHaveLength(1);
});

test("401, 403, 404, and transient failure do not fall back to overview", async ({ page }) => {
  const cases = [
    {
      id: "ws_prompt_401",
      status: 401,
      message: "Synthetic session expired",
      alert: "task-details-prompt-denied",
    },
    {
      id: "ws_prompt_403",
      status: 403,
      message: "Synthetic permission revoked",
      alert: "task-details-prompt-denied",
    },
    {
      id: "ws_prompt_404",
      status: 404,
      message: "Synthetic missing workspace",
      alert: "task-details-prompt-missing",
    },
    {
      id: "ws_prompt_502",
      status: 502,
      message: "Synthetic transient failure",
      alert: "task-details-prompt-error",
    },
  ] as const;
  const reads: string[] = [];
  const items = cases.map((entry) => overviewItem(entry.id, entry.id, OVERVIEW_LEAK));
  await mockAwfConsoleApi(page, {
    mode: "hosted",
    overviewItems: [...items, overviewItem("ws_prompt_error_other", "Unopened card", OVERVIEW_LEAK)],
  });
  await installDetailRoute(page, reads, async (route, id) => {
    const entry = cases.find((candidate) => candidate.id === id);
    await fulfillJson(
      route,
      {
        detail: { message: entry?.message ?? "unscripted", token: REJECTED_SECRET },
        task_prompt: FOREIGN_PROMPT,
      },
      entry?.status ?? 500,
    );
  });

  await openConsole(page, true);
  expect(detailReads(reads)).toEqual([]);

  for (const entry of cases) {
    const dialog = await openDetails(page, entry.id);
    const prompt = dialog.getByTestId("task-details-prompt");
    const alert = prompt.getByTestId(entry.alert);
    await expect(alert).toBeVisible();
    if (entry.status === 404) {
      await expect(alert).toContainText("Workspace detail was not found.");
      await expect(alert).not.toContainText(entry.message);
    } else {
      await expect(alert).toContainText(entry.message);
    }
    await expect(prompt).not.toContainText("No prompt stored for this workspace.");
    await expect(prompt).not.toContainText(OVERVIEW_LEAK);
    await expect(prompt).not.toContainText(FOREIGN_PROMPT);
    await expect(prompt).not.toContainText(REJECTED_SECRET);
    await dialog.getByRole("button", { name: "Close task details" }).click();
  }

  expect(detailReads(reads)).toHaveLength(cases.length);
  expect(detailReads(reads).some((url) => url.includes("ws_prompt_error_other"))).toBe(false);
});

test("network failure does not render a rejected body or overview prompt", async ({ page }) => {
  const reads: string[] = [];
  const item = overviewItem("ws_prompt_network", "Network failure task", OVERVIEW_LEAK);
  await mockAwfConsoleApi(page, {
    mode: "local",
    overviewItems: [item, overviewItem("ws_prompt_network_b", "Other network card", OVERVIEW_LEAK)],
  });
  await installDetailRoute(page, reads, async (route) => {
    await route.abort("failed");
  });

  await openConsole(page, false);
  const dialog = await openDetails(page, item.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt.getByTestId("task-details-prompt-error")).toBeVisible();
  await expect(prompt).not.toContainText("No prompt stored for this workspace.");
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);
  await expect(prompt).not.toContainText(REJECTED_SECRET);
  expect(detailReads(reads)).toHaveLength(1);
});

test("late detail responses cannot paint another workspace or a denied prompt", async ({ page }) => {
  const reads: string[] = [];
  const releases: Array<() => void> = [];
  const alpha = overviewItem("ws_prompt_race_a", "Race alpha", OVERVIEW_LEAK);
  const bravo = overviewItem("ws_prompt_race_b", "Race bravo", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [alpha, bravo] });
  await installDetailRoute(page, reads, async (route, id) => {
    if (id === alpha.workspace_id && releases.length === 0) {
      await new Promise<void>((resolve) => {
        releases.push(resolve);
      });
      await fulfillJson(route, { id, task_prompt: LATE_PROMPT });
      return;
    }
    await fulfillJson(route, {
      id,
      task_prompt: id === bravo.workspace_id ? DETAIL_BRAVO : DETAIL_ALPHA,
    });
  });

  await openConsole(page, true);
  expect(detailReads(reads)).toEqual([]);
  const firstDialog = await openDetails(page, alpha.workspace_id);
  await expect.poll(() => detailReads(reads).length).toBe(1);
  await expect(firstDialog.getByTestId("task-details-prompt").getByRole("status")).toBeVisible();
  await firstDialog.getByRole("button", { name: "Close task details" }).click();
  await expect(firstDialog).toBeHidden();

  const secondDialog = await openDetails(page, bravo.workspace_id);
  const prompt = secondDialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(DETAIL_BRAVO);
  releases[0]?.();
  await expect(prompt).toContainText(DETAIL_BRAVO);
  await expect(prompt).not.toContainText(LATE_PROMPT);
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);
  expect(detailReads(reads).filter((url) => url.includes(alpha.workspace_id))).toHaveLength(1);
  expect(detailReads(reads).filter((url) => url.includes(bravo.workspace_id))).toHaveLength(1);
});

test("revisiting a workspace does not reuse the previous prompt while authorization is pending", async ({ page }) => {
  const reads: string[] = [];
  const pending: Array<{ id: string; release: () => void }> = [];
  let alphaReads = 0;
  const alpha = overviewItem("ws_prompt_revisit_a", "Revisit alpha", OVERVIEW_LEAK);
  const bravo = overviewItem("ws_prompt_revisit_b", "Revisit bravo", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [alpha, bravo] });
  await installDetailRoute(page, reads, async (route, id) => {
    if (id === alpha.workspace_id) {
      alphaReads += 1;
      if (alphaReads === 1) {
        await fulfillJson(route, { id, task_prompt: DETAIL_ALPHA });
        return;
      }
    }
    await new Promise<void>((resolve) => {
      pending.push({ id, release: resolve });
    });
    if (id === alpha.workspace_id) {
      await fulfillJson(
        route,
        {
          detail: { message: "Synthetic permission revoked", token: REJECTED_SECRET },
          task_prompt: DETAIL_ALPHA,
        },
        403,
      );
      return;
    }
    await fulfillJson(route, { id, task_prompt: DETAIL_BRAVO });
  });

  await openConsole(page, true);
  const dialog = await openDetails(page, alpha.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect(prompt).toContainText(DETAIL_ALPHA);

  // The dialog covers the rail but does not make those Details controls inert,
  // so a second open changes the mounted modal's workspace without remounting it.
  await page
    .getByTestId(`workspace-card-${bravo.workspace_id}`)
    .getByRole("button", { name: "Details", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
    });
  await expect(dialog.getByRole("heading", { name: bravo.title })).toBeVisible();
  await expect.poll(() => detailReads(reads).filter((url) => url.includes(bravo.workspace_id)).length).toBe(1);
  await expect(prompt.getByTestId("task-details-prompt-loading")).toBeVisible();
  await expect(prompt).not.toContainText(DETAIL_ALPHA);

  await page
    .getByTestId(`workspace-card-${alpha.workspace_id}`)
    .getByRole("button", { name: "Details", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
    });
  await expect(dialog.getByRole("heading", { name: alpha.title })).toBeVisible();
  await expect(prompt.getByTestId("task-details-prompt-loading")).toBeVisible();
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText(DETAIL_BRAVO);
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);
  await expect
    .poll(() => detailReads(reads).filter((url) => url.includes(alpha.workspace_id)).length)
    .toBe(2);

  pending.find((hold) => hold.id === alpha.workspace_id)?.release();
  await expect(prompt.getByTestId("task-details-prompt-denied")).toContainText("Synthetic permission revoked");
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText(REJECTED_SECRET);
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);

  pending.find((hold) => hold.id === bravo.workspace_id)?.release();
  await expect(prompt.getByTestId("task-details-prompt-denied")).toContainText("Synthetic permission revoked");
  await expect(prompt).not.toContainText(DETAIL_BRAVO);
  expect(detailReads(reads).filter((url) => url.includes(alpha.workspace_id))).toHaveLength(2);
  expect(detailReads(reads).filter((url) => url.includes(bravo.workspace_id))).toHaveLength(1);
});

test("an in-flight result from an earlier visit cannot refill the same workspace", async ({ page }) => {
  const reads: string[] = [];
  const pending: Array<{ id: string; seq: number; release: () => void }> = [];
  let alphaSeq = 0;
  const alpha = overviewItem("ws_prompt_inflight_a", "In-flight alpha", OVERVIEW_LEAK);
  const bravo = overviewItem("ws_prompt_inflight_b", "In-flight bravo", "");
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: [alpha, bravo] });
  await installDetailRoute(page, reads, async (route, id) => {
    const seq = id === alpha.workspace_id ? ++alphaSeq : 0;
    await new Promise<void>((resolve) => {
      pending.push({ id, seq, release: resolve });
    });
    if (id === alpha.workspace_id && seq === 1) {
      await fulfillJson(route, { id, task_prompt: DETAIL_ALPHA });
      return;
    }
    if (id === alpha.workspace_id) {
      await fulfillJson(
        route,
        {
          detail: { message: "Synthetic permission revoked", token: REJECTED_SECRET },
          task_prompt: DETAIL_ALPHA,
        },
        403,
      );
      return;
    }
    await fulfillJson(route, { id, task_prompt: DETAIL_BRAVO });
  });

  await openConsole(page, true);
  const dialog = await openDetails(page, alpha.workspace_id);
  const prompt = dialog.getByTestId("task-details-prompt");
  await expect.poll(() => detailReads(reads).filter((url) => url.includes(alpha.workspace_id)).length).toBe(1);
  await expect(prompt.getByTestId("task-details-prompt-loading")).toBeVisible();
  await expect(prompt).not.toContainText(DETAIL_ALPHA);

  await page
    .getByTestId(`workspace-card-${bravo.workspace_id}`)
    .getByRole("button", { name: "Details", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
    });
  await expect(dialog.getByRole("heading", { name: bravo.title })).toBeVisible();
  await expect.poll(() => detailReads(reads).filter((url) => url.includes(bravo.workspace_id)).length).toBe(1);

  await page
    .getByTestId(`workspace-card-${alpha.workspace_id}`)
    .getByRole("button", { name: "Details", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
    });
  await expect(dialog.getByRole("heading", { name: alpha.title })).toBeVisible();
  await expect
    .poll(() => detailReads(reads).filter((url) => url.includes(alpha.workspace_id)).length)
    .toBe(2);
  await expect(prompt.getByTestId("task-details-prompt-loading")).toBeVisible();
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText(DETAIL_BRAVO);

  pending.find((hold) => hold.id === alpha.workspace_id && hold.seq === 1)?.release();
  await expect(prompt.getByTestId("task-details-prompt-loading")).toBeVisible();
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);

  pending.find((hold) => hold.id === alpha.workspace_id && hold.seq === 2)?.release();
  await expect(prompt.getByTestId("task-details-prompt-denied")).toContainText("Synthetic permission revoked");
  await expect(prompt).not.toContainText(DETAIL_ALPHA);
  await expect(prompt).not.toContainText(REJECTED_SECRET);

  pending.find((hold) => hold.id === bravo.workspace_id)?.release();
  await expect(prompt.getByTestId("task-details-prompt-denied")).toContainText("Synthetic permission revoked");
  await expect(prompt).not.toContainText(DETAIL_BRAVO);
  expect(detailReads(reads).filter((url) => url.includes(alpha.workspace_id))).toHaveLength(2);
  expect(detailReads(reads).filter((url) => url.includes(bravo.workspace_id))).toHaveLength(1);
});

test("a late 200 after context change or a newer denial does not restore the prompt", async ({ page }) => {
  const reads: string[] = [];
  let releaseLate: (() => void) | undefined;
  let phase: "hold" | "deny" | "foreign" = "hold";
  const item = overviewItem("ws_prompt_context", "Context switch task", OVERVIEW_LEAK);
  await mockAwfConsoleApi(page, {
    mode: "hosted",
    overviewItems: [item, overviewItem("ws_prompt_context_b", "Context other", "")],
  });
  await installDetailRoute(page, reads, async (route, id) => {
    if (id === item.workspace_id && phase === "hold") {
      await new Promise<void>((resolve) => {
        releaseLate = resolve;
      });
      await fulfillJson(route, { id, task_prompt: LATE_PROMPT });
      return;
    }
    if (phase === "deny") {
      await fulfillJson(
        route,
        { detail: { message: "Synthetic permission revoked", token: REJECTED_SECRET }, task_prompt: LATE_PROMPT },
        403,
      );
      return;
    }
    await fulfillJson(route, { id: "ws_other_tenant", task_prompt: FOREIGN_PROMPT });
  });

  await openConsole(page, true);
  const dialog = await openDetails(page, item.workspace_id);
  await expect.poll(() => detailReads(reads).length).toBe(1);
  await expect(dialog.getByTestId("task-details-prompt").getByRole("status")).toBeVisible();

  phase = "deny";
  await page.evaluate(() => {
    const next = new URL(window.location.href);
    next.searchParams.set("project_id", "p_other");
    window.history.replaceState(null, "", `${next.pathname}?${next.searchParams.toString()}`);
  });
  await expect(dialog).toBeHidden();
  releaseLate?.();
  await expect(page.getByText(LATE_PROMPT)).toHaveCount(0);

  const reopened = await openDetails(page, item.workspace_id);
  const prompt = reopened.getByTestId("task-details-prompt");
  await expect(prompt.getByTestId("task-details-prompt-denied")).toContainText("Synthetic permission revoked");
  await expect(prompt).not.toContainText(LATE_PROMPT);
  await expect(prompt).not.toContainText(OVERVIEW_LEAK);
  await expect(prompt).not.toContainText(FOREIGN_PROMPT);
  await expect(prompt).not.toContainText(REJECTED_SECRET);
  const deniedRead = detailReads(reads).find((url) => url.includes("project_id=p_other"));
  expect(deniedRead).toBeTruthy();
  expect(deniedRead).toContain(`/workspaces/${item.workspace_id}`);

  await reopened.getByRole("button", { name: "Close task details" }).click();
  phase = "foreign";
  const mismatch = await openDetails(page, item.workspace_id);
  const mismatchPrompt = mismatch.getByTestId("task-details-prompt");
  await expect(mismatchPrompt.getByTestId("task-details-prompt-error")).toBeVisible();
  await expect(mismatchPrompt).not.toContainText(FOREIGN_PROMPT);
  await expect(mismatchPrompt).not.toContainText("No prompt stored for this workspace.");
  expect(detailReads(reads).length).toBeLessThanOrEqual(4);
  expect(detailReads(reads).some((url) => url.includes("ws_prompt_context_b"))).toBe(false);
});

test("a successful detail is rendered only when its id matches the requested workspace", async ({ page }) => {
  const reads: string[] = [];
  const cases = [
    { id: "ws_prompt_omit_id", body: { task_prompt: FOREIGN_PROMPT } },
    { id: "ws_prompt_empty_id", body: { id: "", task_prompt: FOREIGN_PROMPT } },
    { id: "ws_prompt_blank_id", body: { id: "   ", task_prompt: FOREIGN_PROMPT } },
    { id: "ws_prompt_numeric_id", body: { id: 404, task_prompt: FOREIGN_PROMPT } },
    { id: "ws_prompt_null_id", body: { id: null, task_prompt: FOREIGN_PROMPT } },
  ] as const;
  const items = cases.map((entry) => overviewItem(entry.id, entry.id, OVERVIEW_LEAK));
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: items });
  await installDetailRoute(page, reads, async (route, id) => {
    const entry = cases.find((candidate) => candidate.id === id);
    await fulfillJson(route, entry?.body ?? { id, task_prompt: FOREIGN_PROMPT });
  });

  await openConsole(page, true);
  expect(detailReads(reads)).toEqual([]);

  for (const entry of cases) {
    const dialog = await openDetails(page, entry.id);
    const prompt = dialog.getByTestId("task-details-prompt");
    const alert = prompt.getByTestId("task-details-prompt-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Task prompt response did not match this workspace.");
    await expect(prompt).not.toContainText(FOREIGN_PROMPT);
    await expect(prompt).not.toContainText(OVERVIEW_LEAK);
    await expect(prompt).not.toContainText("No prompt stored for this workspace.");
    await dialog.getByRole("button", { name: "Close task details" }).click();
  }

  expect(detailReads(reads)).toHaveLength(cases.length);
});

test("missing or non-string detail prompts are errors, not an absent prompt", async ({ page }) => {
  const reads: string[] = [];
  const cases = [
    { id: "ws_prompt_omit_prompt", body: { id: "ws_prompt_omit_prompt" } },
    { id: "ws_prompt_null_prompt", body: { id: "ws_prompt_null_prompt", task_prompt: null } },
    { id: "ws_prompt_numeric_prompt", body: { id: "ws_prompt_numeric_prompt", task_prompt: 404 } },
    { id: "ws_prompt_object_prompt", body: { id: "ws_prompt_object_prompt", task_prompt: { text: FOREIGN_PROMPT } } },
  ] as const;
  const items = cases.map((entry) => overviewItem(entry.id, entry.id, OVERVIEW_LEAK));
  await mockAwfConsoleApi(page, { mode: "hosted", overviewItems: items });
  await installDetailRoute(page, reads, async (route, id) => {
    const entry = cases.find((candidate) => candidate.id === id);
    await fulfillJson(route, entry?.body ?? { id, task_prompt: FOREIGN_PROMPT });
  });

  await openConsole(page, true);
  expect(detailReads(reads)).toEqual([]);

  for (const entry of cases) {
    const dialog = await openDetails(page, entry.id);
    const prompt = dialog.getByTestId("task-details-prompt");
    const alert = prompt.getByTestId("task-details-prompt-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Task prompt response was malformed.");
    await expect(prompt).not.toContainText(FOREIGN_PROMPT);
    await expect(prompt).not.toContainText(OVERVIEW_LEAK);
    await expect(prompt).not.toContainText("No prompt stored for this workspace.");
    await dialog.getByRole("button", { name: "Close task details" }).click();
  }

  expect(detailReads(reads)).toHaveLength(cases.length);
});
