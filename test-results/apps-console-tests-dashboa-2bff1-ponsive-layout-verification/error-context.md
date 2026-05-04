# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: apps/console/tests/dashboard-inspector.spec.ts >> Dashboard Workspace Inspector >> Responsive layout verification
- Location: apps/console/tests/dashboard-inspector.spec.ts:114:7

# Error details

```
Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
Call log:
  - navigating to "/?workspaceId=ws_mock123", waiting until "load"

```

# Test source

```ts
  15  |     await page.route("/api/awf/metrics/workspaces/summary", async (route) => {
  16  |       await route.fulfill({ json: { active: 1, failed: 0 } });
  17  |     });
  18  | 
  19  |     await page.route("/api/awf/merge-queue*", async (route) => {
  20  |       await route.fulfill({ json: { items: [], has_more: false } });
  21  |     });
  22  | 
  23  |     await page.route("/api/awf/metrics/failures/summary", async (route) => {
  24  |       await route.fulfill({ json: { taxonomy: [], latest_examples: [], total_failures: 0 } });
  25  |     });
  26  | 
  27  |     const mockWorkspace = {
  28  |       workspace_id: "ws_mock123",
  29  |       title: "Mock Workspace",
  30  |       repo_url: "https://github.com/test/repo",
  31  |       base_branch: "main",
  32  |       agent: "test-agent",
  33  |       status: "running",
  34  |       created_at: new Date().toISOString(),
  35  |       updated_at: new Date().toISOString(),
  36  |       lifecycle: [],
  37  |       llm_usage: null,
  38  |       recovery: null,
  39  |     };
  40  | 
  41  |     await page.route("/api/awf/workspaces/overview*", async (route) => {
  42  |       await route.fulfill({ json: { items: [mockWorkspace], has_more: false } });
  43  |     });
  44  | 
  45  |     await page.route("/api/awf/workspaces/ws_mock123", async (route) => {
  46  |       await route.fulfill({ json: mockWorkspace });
  47  |     });
  48  | 
  49  |     await page.route("/api/awf/workspaces/ws_mock123/runtime", async (route) => {
  50  |       await route.fulfill({ json: { status: "running" } });
  51  |     });
  52  | 
  53  |     await page.route("/api/awf/workspaces/ws_mock123/events*", async (route) => {
  54  |       await route.fulfill({ json: { items: [], has_more: false } });
  55  |     });
  56  | 
  57  |     await page.route("/api/awf/workspaces/ws_mock123/operations*", async (route) => {
  58  |       await route.fulfill({ json: { items: [], has_more: false } });
  59  |     });
  60  | 
  61  |     await page.route("/api/awf/workspaces/ws_mock123/logs", async (route) => {
  62  |       await route.fulfill({ json: { items: [], has_more: false } });
  63  |     });
  64  |   });
  65  | 
  66  |   test("Open and close inspector, verify URL persistence, and test no jump", async ({ page }) => {
  67  |     // 1. Initial Load
  68  |     await page.goto("/");
  69  |     
  70  |     // Wait for the workspace list to load and select the workspace
  71  |     const workspaceRow = page.locator("text=ws_mock123").first();
  72  |     await workspaceRow.waitFor({ state: "visible" });
  73  |     await workspaceRow.click();
  74  | 
  75  |     // Wait for inspector to open
  76  |     const inspector = page.locator("h2", { hasText: "Mock Workspace" }).first();
  77  |     await inspector.waitFor({ state: "visible" });
  78  | 
  79  |     // Verify URL was updated to include workspaceId
  80  |     await expect(page).toHaveURL(/workspaceId=ws_mock123/);
  81  | 
  82  |     // Measure global pane to check for jumps later
  83  |     const capacityPanel = page.locator("text=Resource / Capacity").first();
  84  |     await capacityPanel.waitFor({ state: "visible" });
  85  |     const initialBox = await capacityPanel.boundingBox();
  86  | 
  87  |     // 2. Close Inspector
  88  |     const closeBtn = page.getByRole("button", { name: "Close inspector" });
  89  |     await closeBtn.click();
  90  | 
  91  |     // Verify inspector is closed (we used hidden for the overlay and translate-x-full)
  92  |     // Wait for the class to update
  93  |     await expect(page.locator(".fixed.inset-y-0.right-0").first()).toHaveClass(/translate-x-full/);
  94  | 
  95  |     // Verify URL updated to remove workspaceId
  96  |     await expect(page).not.toHaveURL(/workspaceId/);
  97  | 
  98  |     // 3. No Jump Validation
  99  |     const afterBox = await capacityPanel.boundingBox();
  100 |     expect(initialBox?.width).toBeCloseTo(afterBox!.width, 1);
  101 |     expect(initialBox?.height).toBeCloseTo(afterBox!.height, 1);
  102 |     expect(initialBox?.x).toBeCloseTo(afterBox!.x, 1);
  103 |     expect(initialBox?.y).toBeCloseTo(afterBox!.y, 1);
  104 | 
  105 |     // 4. Persistence on Reload
  106 |     // Navigate manually to the URL with the ID
  107 |     await page.goto("/?workspaceId=ws_mock123");
  108 |     
  109 |     // Wait for inspector to be visible (translate-x-0)
  110 |     await expect(page.locator(".fixed.inset-y-0.right-0").first()).toHaveClass(/translate-x-0/);
  111 |     await expect(page.locator("h2", { hasText: "Mock Workspace" }).first()).toBeVisible();
  112 |   });
  113 | 
  114 |   test("Responsive layout verification", async ({ page }) => {
> 115 |     await page.goto("/?workspaceId=ws_mock123");
      |                ^ Error: page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
  116 |     const inspectorDrawer = page.locator(".fixed.inset-y-0.right-0").first();
  117 |     
  118 |     // Desktop layout (default is 1280x720)
  119 |     await expect(inspectorDrawer).toHaveClass(/sm:w-\[600px\]/);
  120 |     await expect(inspectorDrawer).toHaveClass(/xl:w-\[800px\]/);
  121 | 
  122 |     // Mobile layout
  123 |     await page.setViewportSize({ width: 375, height: 667 });
  124 |     
  125 |     // In mobile, it should be full width (w-full is on it) and overlay should be visible
  126 |     const overlay = page.locator(".fixed.inset-0.z-40").first();
  127 |     await expect(overlay).toBeVisible();
  128 |     await expect(inspectorDrawer).toHaveClass(/w-full/);
  129 |   });
  130 | });
  131 | 
```