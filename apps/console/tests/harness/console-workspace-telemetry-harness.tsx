"use client";

import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { ConsoleWorkspaceTelemetry } from "@/components/console-workspace-telemetry";
import {
  parseTelemetryPresentation,
  projectWorkspaceTelemetryView,
  type TelemetryViewWindow,
} from "@/lib/console-workspace-telemetry";

import successFixture from "@/lib/fixtures/console-workspace-telemetry/success.json";
import partialFixture from "@/lib/fixtures/console-workspace-telemetry/partial.json";
import staleFixture from "@/lib/fixtures/console-workspace-telemetry/stale.json";
import unallocatedFixture from "@/lib/fixtures/console-workspace-telemetry/unallocated.json";

const FIXTURES: Record<string, unknown> = {
  success: successFixture,
  partial: partialFixture,
  stale: staleFixture,
  unallocated: unallocatedFixture,
};

const LONG_WORKSPACE_ID =
  "ws_very_long_workspace_identifier_9867b7a106d341df8af53b7e_stage3_telemetry";
const LONG_MODEL_LABEL =
  "provider/models/example-large-model-name-with-extra-long-suffix-v2";

/**
 * Test-only harness that mounts the real ConsoleWorkspaceTelemetry component.
 * Not linked from product UI; served only via page.harness.tsx when the
 * AWF_CONSOLE_TEST_HARNESS build entry is enabled (Playwright).
 */
export function ConsoleWorkspaceTelemetryHarness() {
  const searchParams = useSearchParams();
  const fixtureName = searchParams.get("fixture") ?? "success";
  const capabilitiesAbsent = searchParams.get("capabilities") === "absent";
  const modeParam = searchParams.get("mode");
  const mode = modeParam === "historical" ? "historical" : "live";
  const requestError = searchParams.get("error");
  const lastGoodAt = searchParams.get("lastGood");
  const unknownLimits = searchParams.get("unknownLimits") === "1";
  const admittedPartial = searchParams.get("admittedPartial") === "1";
  const nowMsParam = searchParams.get("nowMs");
  const nowMs = nowMsParam ? Number(nowMsParam) : Date.parse("2026-09-12T12:01:00+00:00");

  const [selectedView, setSelectedView] = useState<TelemetryViewWindow>("1h");
  const [viewChangeCount, setViewChangeCount] = useState(0);
  const [lastView, setLastView] = useState<string | null>(null);

  const viewModel = useMemo(() => {
    if (capabilitiesAbsent) {
      return null;
    }
    let raw = structuredClone(FIXTURES[fixtureName] ?? FIXTURES.success);
    if (
      (unknownLimits || admittedPartial) &&
      raw &&
      typeof raw === "object" &&
      raw !== null
    ) {
      const envelope = raw as {
        admitted?: {
          cpu_limit_cores?: string | null;
          memory_limit_bytes?: number | null;
          partial?: boolean;
        } | null;
      };
      if (envelope.admitted) {
        if (unknownLimits) {
          envelope.admitted.cpu_limit_cores = null;
          envelope.admitted.memory_limit_bytes = null;
        }
        if (admittedPartial) {
          envelope.admitted.partial = true;
        }
      }
    }
    const parsed = parseTelemetryPresentation(raw);
    if (!parsed) {
      return null;
    }
    return projectWorkspaceTelemetryView(parsed, { nowMs });
  }, [capabilitiesAbsent, fixtureName, unknownLimits, admittedPartial, nowMs]);

  const onViewChange = useCallback((view: TelemetryViewWindow) => {
    setSelectedView(view);
    setViewChangeCount((n) => n + 1);
    setLastView(view);
  }, []);

  return (
    <div
      className="min-h-screen bg-canvas p-4 text-fg"
      data-testid="telemetry-harness-root"
      data-view-changes={viewChangeCount}
      data-last-view={lastView ?? ""}
    >
      <h1 className="mb-3 text-sm font-semibold text-fg">Workspace telemetry harness</h1>
      {capabilitiesAbsent || viewModel == null ? (
        <div data-testid="telemetry-harness-empty" />
      ) : (
        <ConsoleWorkspaceTelemetry
          viewModel={viewModel}
          selectedView={selectedView}
          onViewChange={onViewChange}
          mode={mode}
          lastGoodAt={lastGoodAt}
          requestError={requestError}
          available={!capabilitiesAbsent}
          workspaceId={LONG_WORKSPACE_ID}
          modelLabel={LONG_MODEL_LABEL}
        />
      )}
    </div>
  );
}
