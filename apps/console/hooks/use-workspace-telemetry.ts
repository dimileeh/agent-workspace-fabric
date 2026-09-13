"use client";

import { useEffect, useState, type MutableRefObject } from "react";
import { parseApiResponse } from "@/components/console-dashboard-shared";
import { workspaceTelemetryPath } from "@/lib/console-urls";
import { parseTelemetryPresentation, type ParsedTelemetryPresentation, type TelemetryViewWindow } from "@/lib/console-workspace-telemetry";

export type TelemetryReadState = {
  data: ParsedTelemetryPresentation | null;
  error: string | null;
  lastGoodAt: string | null;
};

/** Mounted for exactly one authorized workspace/view/gate identity. No response cache. */
export function useWorkspaceTelemetry({ workspaceId, view, url, authEpoch, epochRef, onDenied }: {
  workspaceId: string;
  view: TelemetryViewWindow;
  url: string;
  authEpoch: number;
  epochRef: MutableRefObject<number>;
  onDenied: () => void;
}) {
  const [state, setState] = useState<TelemetryReadState>({ data: null, error: null, lastGoodAt: null });
  useEffect(() => {
    let disposed = false;
    let denied = false;
    let timer = 0;
    let resourceIdentity: string | null = null;
    let controller: AbortController | null = null;
    const current = () => !disposed && !denied && epochRef.current === authEpoch && workspaceTelemetryPath(workspaceId, view) === url;
    const read = async () => {
      if (!current()) return;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 30_000);
      try {
        const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
        if (!current()) return;
        // Denial invalidates retained data even if reading the error body fails.
        if (response.status === 401 || response.status === 403) {
          denied = true;
          setState({ data: null, error: "Telemetry access denied", lastGoodAt: null });
          onDenied();
          return;
        }
        const result = await parseApiResponse<unknown>(response);
        if (!current()) return;
        if (!result.ok) {
          throw new Error(result.message || "Telemetry request failed");
        }
        const raw = result.data as Record<string, unknown> | null;
        // Ownership is private validation evidence, never UI text or routing authority.
        const owner = raw?.ownership as Record<string, unknown> | null | undefined;
        const query = new URL(url, window.location.origin).searchParams;
        if (owner && (typeof owner !== "object" || Array.isArray(owner) ||
          owner.workspace_record_id !== workspaceId ||
          !Number.isSafeInteger(owner.placement_attempt) || Number(owner.placement_attempt) < 0 ||
          typeof owner.provider_resource_uid !== "string" || !owner.provider_resource_uid || owner.provider_resource_uid.length > 64 ||
          ["org_id", "project_id"].some(key => query.has(key) && owner[key] !== query.get(key)))) {
          setState({ data: null, error: "Telemetry ownership mismatch", lastGoodAt: null });
          return;
        }
        const nextIdentity = owner ? JSON.stringify([owner.provider_resource_uid, owner.placement_attempt]) : null;
        if (owner && nextIdentity !== resourceIdentity) {
          resourceIdentity = nextIdentity;
          setState({ data: null, error: null, lastGoodAt: null });
        }
        const parsed = parseTelemetryPresentation(raw);
        if (!parsed || parsed.view !== view) throw new Error("Malformed telemetry response");
        setState({ data: parsed, error: null, lastGoodAt: new Date().toISOString() });
      } catch (error) {
        if (current()) setState(previous => ({ ...previous, error: error instanceof Error ? error.message : "Telemetry request failed" }));
      } finally {
        window.clearTimeout(timeout);
        if (current()) timer = window.setTimeout(() => { void read(); }, 60_000);
      }
    };
    // Let effect replay/cleanup invalidate the first setup before any network read.
    void Promise.resolve().then(read);
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); };
  }, [workspaceId, view, url, authEpoch, epochRef, onDenied]);
  return state;
}
