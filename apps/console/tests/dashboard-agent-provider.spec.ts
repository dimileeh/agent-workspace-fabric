import { expect, test, type Page } from "@playwright/test";

import { fulfillJson, mockAwfConsoleApi } from "./fixtures/console-api";

const model = "moonshotai/kimi-k2.7-code";

function workspace(provider: string) {
  return {
    workspace_id: `ws_provider_${provider}`,
    task_id: `task_provider_${provider}`,
    title: `${provider} workspace`,
    task_prompt: "Provider visibility regression",
    repo_url: "https://github.com/example/awf.git",
    base_branch: "development",
    branch_name: "awf/provider-visibility",
    agent: "codex",
    agent_provider: provider,
    agent_model: model,
    agent_effort: null,
    agent_model_source: "task_policy",
    agent_effort_source: "unavailable",
    status: "completed",
    current_phase: "completed",
    active_operation: null,
    lifecycle: [],
    llm_usage: null,
    recovery: null,
    coordination_warnings: [],
    pr_url: null,
    pr_number: null,
    created_at: "2026-11-10T02:00:00Z",
    updated_at: "2026-11-10T03:00:00Z",
  };
}

async function mockWorkspace(page: Page, provider: string, detailProvider: string | null | undefined) {
  const item = workspace(provider);
  await mockAwfConsoleApi(page, { overviewItems: [item] });
  await page.route(`/api/awf/workspaces/${item.workspace_id}`, async (route) => {
    await fulfillJson(route, { ...item, id: item.workspace_id, agent_provider: detailProvider });
  });
  await page.route(`/api/awf/workspaces/${item.workspace_id}/logs`, async (route) => {
    await fulfillJson(route, { items: [], has_more: false });
  });
  return item;
}

for (const [provider, label] of [["openrouter", "OpenRouter"], ["ollama", "Ollama"]]) {
  test(`${label} is visible in cards, inspector, task details and fullscreen logs`, async ({ page }) => {
    const item = await mockWorkspace(page, provider, provider);
    const identity = `codex · ${label} · ${model}`;
    await page.goto("/");
    const card = page.getByTestId(`workspace-card-${item.workspace_id}`);
    await expect(card.getByText(identity, { exact: true })).toBeVisible();
    await expect(card.getByText(identity, { exact: true })).toHaveAttribute("title", new RegExp(`provider ${label}`));

    await card.getByRole("button", { name: "Details", exact: true }).click();
    const details = page.getByRole("dialog", { name: /Task details/i });
    await expect(details.getByText(identity, { exact: true })).toBeVisible();
    await details.getByRole("button", { name: "Close task details" }).click();

    await card.getByRole("button", { name: "Logs", exact: true }).click();
    const logs = page.locator(".fixed.inset-0.z-50");
    await expect(logs.getByText(`${identity} / completed / stream`, { exact: false })).toBeVisible();
    await logs.getByRole("button", { name: "Close", exact: true }).click();

    await card.getByRole("button", { name: `Open workspace details for ${item.title}`, exact: true }).click();
    const inspector = page.locator(".fixed.inset-y-0.right-0").first();
    await expect(inspector.getByText(identity, { exact: true })).toBeVisible();
  });
}

test("inspector prefers the detail provider over the overview provider", async ({ page }) => {
  const item = await mockWorkspace(page, "openrouter", "ollama");
  await page.goto(`/?workspaceId=${item.workspace_id}`);
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(`codex · Ollama · ${model}`, { exact: true })).toBeVisible();
  await expect(inspector.getByText(`codex · OpenRouter · ${model}`, { exact: true })).toHaveCount(0);
});

test("an explicitly unknown detail provider does not inherit a stale overview provider", async ({ page }) => {
  const item = await mockWorkspace(page, "openrouter", null);
  await page.goto(`/?workspaceId=${item.workspace_id}`);
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(`codex · ${model}`, { exact: true })).toBeVisible();
  await expect(inspector.getByText(`codex · OpenRouter · ${model}`, { exact: true })).toHaveCount(0);
});

test("inspector retains overview provider when an older detail omits the field", async ({ page }) => {
  const item = await mockWorkspace(page, "openrouter", undefined);
  await page.goto(`/?workspaceId=${item.workspace_id}`);
  const inspector = page.locator(".fixed.inset-y-0.right-0").first();
  await expect(inspector.getByText(`codex · OpenRouter · ${model}`, { exact: true })).toBeVisible();
});
