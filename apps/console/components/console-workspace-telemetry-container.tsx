"use client";

import { useEffect, useMemo, useState, type MutableRefObject } from "react";
import { capabilityIdentityKey, isWidgetAvailable } from "@/lib/console-capabilities";
import { workspaceTelemetryPath } from "@/lib/console-urls";
import {
  projectWorkspaceTelemetryView,
  TELEMETRY_VIEWS,
  type TelemetryViewWindow,
} from "@/lib/console-workspace-telemetry";
import type { ConsoleCapabilities } from "@/lib/types";
import { useWorkspaceTelemetry } from "@/hooks/use-workspace-telemetry";
import { ConsoleWorkspaceTelemetry } from "./console-workspace-telemetry";
import { Panel } from "./console-dashboard-shared";

type Props = {
  workspaceId: string;
  capabilities: ConsoleCapabilities | null;
  ready: boolean;
  authEpoch: number;
  epochRef: MutableRefObject<number>;
  onDenied: () => void;
};

export function ConsoleWorkspaceTelemetryContainer(props: Props) {
  const { capabilities, ready, workspaceId, authEpoch } = props;
  const gates = (["telemetry", "allocation", "cost"] as const).map(
    id => ready && isWidgetAvailable(capabilities, id),
  );
  if (!capabilities || !gates.some(Boolean)) return null;
  const key = JSON.stringify([
    capabilityIdentityKey(capabilities), workspaceTelemetryPath(workspaceId), authEpoch, gates,
  ]);
  return <SelectedTelemetry key={key} {...props} gates={gates} />;
}

function SelectedTelemetry(props: Props & { gates: boolean[] }) {
  const [view, setView] = useState<TelemetryViewWindow>("1h");
  return <TelemetryRead key={view} {...props} view={view} onViewChange={setView} />;
}

function TelemetryRead({
  workspaceId, authEpoch, epochRef, onDenied, gates, view, onViewChange,
}: Props & {
  gates: boolean[];
  view: TelemetryViewWindow;
  onViewChange: (view: TelemetryViewWindow) => void;
}) {
  const url = workspaceTelemetryPath(workspaceId, view);
  const state = useWorkspaceTelemetry({ workspaceId, view, url, authEpoch, epochRef, onDenied });
  const [now, setNow] = useState(() => Date.now());
  // Age last-success data independently of network success, without extra reads.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const model = useMemo(
    () => state.data ? projectWorkspaceTelemetryView(state.data, { nowMs: now }) : null,
    [state.data, now],
  );
  if (!model) {
    return (
      <Panel
        icon={null}
        title="Workspace resources"
        action={
          <div role="tablist" aria-label="Telemetry window">
            {TELEMETRY_VIEWS.map(v => (
              <button
                className="px-2 text-xs"
                key={v}
                role="tab"
                aria-selected={view === v}
                onClick={() => onViewChange(v)}
                data-testid={`telemetry-view-${v}`}
              >
                {v}
              </button>
            ))}
          </div>
        }
      >
        <div
          className="text-xs text-fg-muted"
          data-testid={state.error ? "telemetry-request-error" : "telemetry-loading"}
        >
          {state.error || "Loading workspace resources…"}
        </div>
      </Panel>
    );
  }
  return (
    <ConsoleWorkspaceTelemetry
      viewModel={model}
      selectedView={view}
      onViewChange={onViewChange}
      requestError={state.error}
      lastGoodAt={state.error ? state.lastGoodAt : null}
      showTelemetry={gates[0]}
      showAllocation={gates[1]}
      showCost={gates[2]}
    />
  );
}
