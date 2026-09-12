import { Suspense } from "react";
import { notFound } from "next/navigation";

import { ConsoleWorkspaceTelemetryHarness } from "@/tests/harness/console-workspace-telemetry-harness";

/**
 * Env-gated test harness route. Returns 404 unless AWF_CONSOLE_TEST_HARNESS=1
 * (Playwright webServer). Not linked from product UI — not a public debug surface.
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
