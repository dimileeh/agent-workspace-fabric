import type {
  ConsoleCapabilities,
  ConsoleCapabilityItem,
  ConsoleControlId,
  ConsoleDiagnosticId,
  ConsoleWidgetId,
  WorkspaceOperatorAction,
} from "./types.ts";

export const CONSOLE_SCHEMA_VERSION = 1;

export type CapabilityParseResult =
  | { ok: true; capabilities: ConsoleCapabilities; identityKey: string }
  | {
      ok: false;
      kind: "missing" | "malformed" | "unknown_version" | "auth_denied" | "outage";
      message: string;
      status?: number;
    };

export function capabilityIdentityKey(capabilities: ConsoleCapabilities): string {
  const identity = capabilities.identity;
  return [
    capabilities.backend_kind,
    identity?.backend_id ?? "",
    identity?.scope ?? "",
    identity?.tenant_id ?? "",
  ].join("|");
}

export function parseConsoleCapabilities(
  payload: unknown,
  options?: { status?: number },
): CapabilityParseResult {
  const status = options?.status;
  if (status === 401 || status === 403) {
    return {
      ok: false,
      kind: "auth_denied",
      message: "Console capabilities authorization denied.",
      status,
    };
  }
  if (payload == null) {
    return { ok: false, kind: "missing", message: "Console capabilities response missing." };
  }
  if (typeof payload !== "object" || Array.isArray(payload)) {
    return { ok: false, kind: "malformed", message: "Console capabilities payload malformed." };
  }
  const record = payload as Record<string, unknown>;
  if (typeof record.schema_version !== "number") {
    return { ok: false, kind: "malformed", message: "Console capabilities missing schema_version." };
  }
  if (record.schema_version !== CONSOLE_SCHEMA_VERSION) {
    return {
      ok: false,
      kind: "unknown_version",
      message: `Unsupported console schema_version=${String(record.schema_version)}.`,
    };
  }
  if (
    record.backend_kind !== "local" &&
    record.backend_kind !== "hosted"
  ) {
    return { ok: false, kind: "malformed", message: "Console capabilities backend_kind invalid." };
  }
  if (
    !Array.isArray(record.widgets) ||
    !Array.isArray(record.diagnostics) ||
    !Array.isArray(record.controls)
  ) {
    return { ok: false, kind: "malformed", message: "Console capabilities collections malformed." };
  }
  for (const item of [...record.widgets, ...record.diagnostics] as ConsoleCapabilityItem[]) {
    if (item?.availability === "available" && item.route) {
      if (typeof item.route !== "string" || !item.route.startsWith("/v1/") || item.route.includes("://")) {
        return {
          ok: false,
          kind: "malformed",
          message: "Console capability routes must be relative /v1/... paths.",
        };
      }
    }
  }
  const capabilities = payload as ConsoleCapabilities;
  return {
    ok: true,
    capabilities,
    identityKey: capabilityIdentityKey(capabilities),
  };
}

function findItem(
  items: ConsoleCapabilityItem[] | undefined,
  id: string,
): ConsoleCapabilityItem | undefined {
  return items?.find((item) => item.id === id);
}

export function isWidgetAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleWidgetId,
): boolean {
  const item = findItem(capabilities?.widgets, id);
  return item?.availability === "available";
}

export function isDiagnosticAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): boolean {
  const item = findItem(capabilities?.diagnostics, id);
  return item?.availability === "available";
}

export function widgetRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleWidgetId,
): string | null {
  const item = findItem(capabilities?.widgets, id);
  if (item?.availability !== "available" || !item.route) {
    return null;
  }
  return item.route;
}

export function diagnosticRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): string | null {
  const item = findItem(capabilities?.diagnostics, id);
  if (item?.availability !== "available" || !item.route) {
    return null;
  }
  return item.route;
}

export function controlCapability(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleControlId | WorkspaceOperatorAction,
): ConsoleCapabilityItem | undefined {
  return findItem(capabilities?.controls, id);
}

export function isControlAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleControlId | WorkspaceOperatorAction,
): boolean {
  return controlCapability(capabilities, id)?.availability === "available";
}

export function controlUnsupportedReason(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleControlId | WorkspaceOperatorAction,
): string | null {
  const item = controlCapability(capabilities, id);
  if (!item || item.availability === "available") {
    return null;
  }
  return item.message ?? item.reason_code ?? "unsupported by backend";
}

/** Convert absolute /v1/... capability route to console BFF path (/api/awf/...). */
export function capabilityRouteToAwfPath(route: string): string {
  if (route.startsWith("/v1/")) {
    return `/api/awf/${route.slice("/v1/".length)}`;
  }
  return route;
}
