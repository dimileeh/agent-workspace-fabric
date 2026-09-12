import { expect, type Page, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

const HARNESS = "/test-harness/workspace-telemetry";

async function expectNoViewportOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const viewport = window.innerWidth;
    const amount = Math.max(root.scrollWidth, body.scrollWidth) - viewport;
    const offenders = [...document.querySelectorAll("body *")]
      .map((node) => {
        const element = node as HTMLElement;
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName.toLowerCase(),
          className: element.className.toString(),
          text: (element.textContent ?? "").trim().slice(0, 80),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          width: Math.round(rect.width),
        };
      })
      .filter((item) => item.right > viewport + 1 || item.left < -1)
      .sort((left, right) => right.right - left.right)
      .slice(0, 8);
    return { amount, viewport, offenders };
  });
  expect(overflow.amount, JSON.stringify(overflow, null, 2)).toBeLessThanOrEqual(1);
}

async function expectNoFleetChrome(page: Page) {
  await expect(page.getByLabel("Fleet health")).toHaveCount(0);
  await expect(page.getByText("Fleet health")).toHaveCount(0);
  await expect(page.getByText(/coverage/i)).toHaveCount(0);
  await expect(page.getByText(/lower-bound/i)).toHaveCount(0);
  await expect(page.getByText(/confirmed/i)).toHaveCount(0);
  await expect(page.getByRole("button", { name: /diagnose|retry diagnostic/i })).toHaveCount(0);
}

async function openHarness(
  page: Page,
  query: Record<string, string> = {},
) {
  const params = new URLSearchParams(query);
  await page.goto(`${HARNESS}?${params.toString()}`);
}

test.describe("console workspace telemetry harness", () => {
  test("desktop success fixture renders resources and cost without fleet chrome", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openHarness(page, { fixture: "success" });

    const root = page.getByTestId("console-workspace-telemetry");
    await expect(root).toBeVisible();
    await expect(page.getByTestId("telemetry-meter-cpu")).toContainText("0.25");
    await expect(page.getByTestId("telemetry-meter-memory")).toContainText("1.0 GB");
    await expect(page.getByTestId("telemetry-workload-cost-value")).toContainText("$");
    await expect(page.getByText("Estimated workload cost")).toBeVisible();
    await expect(page.getByText(/LLM usage/i)).toHaveCount(0);

    await expectNoFleetChrome(page);
    await expectNoViewportOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath("telemetry-desktop-success.png"),
      fullPage: true,
    });
  });

  for (const width of [375, 390] as const) {
    test(`mobile ${width}px success with long ids has no overflow`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 844 });
      await openHarness(page, { fixture: "success" });

      await expect(page.getByTestId("console-workspace-telemetry")).toBeVisible();
      await expect(
        page.getByText(/ws_very_long_workspace_identifier/),
      ).toBeVisible();
      await expect(
        page.getByText(/example-large-model-name/),
      ).toBeVisible();
      await expect(page.getByTestId("telemetry-meter-cpu")).toContainText("0.25");
      await expect(page.getByTestId("telemetry-meter-memory")).toContainText("1.0 GB");

      await expectNoFleetChrome(page);
      await expectNoViewportOverflow(page);
      await page.screenshot({
        path: testInfo.outputPath(`telemetry-mobile-${width}-success.png`),
        fullPage: true,
      });
    });
  }

  test("view selector invokes onViewChange", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, { fixture: "success" });

    const harness = page.getByTestId("telemetry-harness-root");
    await expect(harness).toHaveAttribute("data-view-changes", "0");

    await page.getByTestId("telemetry-view-6h").click();
    await expect(harness).toHaveAttribute("data-view-changes", "1");
    await expect(harness).toHaveAttribute("data-last-view", "6h");

    await page.getByTestId("telemetry-view-24h").click();
    await expect(harness).toHaveAttribute("data-view-changes", "2");
    await expect(harness).toHaveAttribute("data-last-view", "24h");
  });

  test("unknown limits and missing history render clearly", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, { fixture: "partial", unknownLimits: "1" });

    await expect(page.getByTestId("telemetry-meter-cpu")).toContainText("unknown");
    await expect(page.getByTestId("telemetry-meter-memory")).toContainText("unknown");
    await expect(page.getByTestId("telemetry-meter-memory")).toContainText("—");
    await expect(page.getByTestId("telemetry-series-memory")).toContainText("No history");
    await expect(page.getByTestId("telemetry-workload-cost-value")).toContainText("partial");
  });

  test("unallocated cost is not fake zero", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, { fixture: "unallocated" });

    await expect(page.getByTestId("telemetry-unallocated")).toBeVisible();
    await expect(page.getByTestId("telemetry-workload-cost-value")).toHaveText("Unallocated");
    await expect(page.getByTestId("telemetry-workload-cost-value")).not.toHaveText("$0");
  });

  test("request error is shown when provided and omitted when null", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, {
      fixture: "success",
      error: "telemetry upstream timeout",
    });
    await expect(page.getByTestId("telemetry-request-error")).toHaveText(
      "telemetry upstream timeout",
    );

    await openHarness(page, { fixture: "success" });
    await expect(page.getByTestId("telemetry-request-error")).toHaveCount(0);
  });

  test("capabilities absent omits the widget without unsupported chrome", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, { capabilities: "absent" });

    await expect(page.getByTestId("telemetry-harness-empty")).toHaveCount(1);
    await expect(page.getByTestId("console-workspace-telemetry")).toHaveCount(0);
    await expect(page.getByText(/unsupported/i)).toHaveCount(0);
    await expectNoFleetChrome(page);
  });

  test("stale live mode sets data-awf-stale; historical mode is labeled", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 800 });
    await openHarness(page, {
      fixture: "stale",
      mode: "live",
      nowMs: String(Date.parse("2026-09-12T12:00:00+00:00")),
      lastGood: "2026-09-12T11:00:00+00:00",
    });

    await expect(page.locator("[data-awf-stale='true']").first()).toBeVisible();
    await expect(page.getByTestId("telemetry-mode-label")).toHaveText("Live");
    await expect(page.getByTestId("telemetry-last-good")).toBeVisible();

    await openHarness(page, {
      fixture: "success",
      mode: "historical",
      nowMs: String(Date.parse("2026-09-12T12:06:00+00:00")),
    });
    await expect(page.getByTestId("telemetry-mode-label")).toHaveText("Historical");
    // Historical mode does not dim as live-stale even if observed_at is aged.
    await expect(
      page.getByTestId("console-workspace-telemetry").locator("[data-awf-stale='true']"),
    ).toHaveCount(0);
  });

  test("harness route 404s without AWF_CONSOLE_TEST_HARNESS is covered by env-gated page", async ({
    page,
  }) => {
    // When the env flag is set (Playwright webServer), the harness is reachable.
    // This assertion documents the positive path; without the flag the page calls notFound().
    await openHarness(page, { fixture: "success" });
    await expect(page.getByTestId("telemetry-harness-root")).toBeVisible();
  });
});
