import { Suspense } from "react";
import { notFound } from "next/navigation";

import { ConsoleWorkspaceTelemetryHarness } from "@/tests/harness/console-workspace-telemetry-harness";

/**
 * Test-only App Router entry (`page.harness.tsx`). Registered via
 * `pageExtensions` only when AWF_CONSOLE_TEST_HARNESS=1 in a non-production
 * build (Playwright webServer), so it is outside the production route graph.
 * Runtime check is defense in depth if the build entry is enabled.
 */
export default function WorkspaceTelemetryHarnessPage() {
  if (process.env.AWF_CONSOLE_TEST_HARNESS !== "1") {
    notFound();
  }

  return (
    <Suspense fallback={<div data-testid="telemetry-harness-loading">Loading…</div>}>
      <ConsoleWorkspaceTelemetryHarness />
    </Suspense>
  );
}
