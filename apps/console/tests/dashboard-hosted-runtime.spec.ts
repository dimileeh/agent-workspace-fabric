import { expect, type Page, test } from "@playwright/test";

import { mockAwfConsoleApi } from "./fixtures/console-api";

async function waitForConsoleReady(page: Page) {
  await expect(page.locator("header").filter({ hasText: "AWF Console" })).toBeVisible();
  await expect(page.getByText("API: ok")).toBeVisible();
}

test("hosted mode shows Cloud Runtime and omits local capacity", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "hosted" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await waitForConsoleReady(page);

  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await expect(page.getByText("within_quota")).toBeVisible();
  await expect(page.getByText(/Cost|Billing|\$/i)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Resource / Runtime Capacity" })).toHaveCount(0);

  await page.screenshot({ path: "test-results/hosted-runtime-desktop.png", fullPage: true });
});

test("hosted mode mobile screenshot", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "hosted" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toBeVisible();
  await page.screenshot({ path: "test-results/hosted-runtime-mobile.png", fullPage: true });
});

test("local mode keeps resource capacity panel", async ({ page }) => {
  await mockAwfConsoleApi(page, { mode: "local" });
  await page.goto("/");
  await waitForConsoleReady(page);
  await expect(page.locator("#awf-capacity")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Cloud Runtime" })).toHaveCount(0);
});
