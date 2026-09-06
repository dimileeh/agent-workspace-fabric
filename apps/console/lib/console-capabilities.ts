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
  const backendId = identity?.backend_id ?? "";
  const scope = identity?.scope ?? "";
  const tenantId = identity?.tenant_id ?? "";
  if (capabilities.backend_kind === "hosted") {
    // Never emit hosted||| — that collapses every tenant onto one epoch key.
    // Parse rejects incomplete hosted identity; this is defense in depth for
    // any direct callers and for race windows around tenant switches.
    if (!backendId || !scope || !tenantId) {
      return `hosted|missing-tenant-discriminator|${backendId}|${scope}|${tenantId}`;
    }
    return ["hosted", backendId, scope, tenantId].join("|");
  }
  return [capabilities.backend_kind, backendId, scope, tenantId].join("|");
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function validateHostedIdentity(identity: unknown): string | null {
  if (identity == null || typeof identity !== "object" || Array.isArray(identity)) {
    return "Hosted console capabilities require identity with a tenant discriminator.";
  }
  const record = identity as Record<string, unknown>;
  if (!isNonEmptyString(record.backend_id)) {
    return "Hosted console capabilities require a non-empty identity.backend_id.";
  }
  if (!isNonEmptyString(record.scope)) {
    return "Hosted console capabilities require a non-empty identity.scope.";
  }
  if (!isNonEmptyString(record.tenant_id)) {
    return "Hosted console capabilities require a non-empty identity.tenant_id.";
  }
  return null;
}

function isRelativeV1Route(route: unknown): route is string {
  return typeof route === "string" && route.startsWith("/v1/") && !route.includes("://");
}

function validateCapabilityEntry(
  item: unknown,
  requireRouteWhenAvailable: boolean,
): string | null {
  if (item == null || typeof item !== "object" || Array.isArray(item)) {
    return "Console capability entry malformed.";
  }
  const record = item as Record<string, unknown>;
  if (typeof record.id !== "string" || record.id.length === 0) {
    return "Console capability entry missing id.";
  }
  if (record.availability !== "available" && record.availability !== "unsupported") {
    return "Console capability availability invalid.";
  }
  if (typeof record.semantics !== "string" || record.semantics.length === 0) {
    return "Console capability entry missing semantics.";
  }
  if (record.availability === "available" && requireRouteWhenAvailable) {
    if (!isRelativeV1Route(record.route)) {
      return "Available console widgets/diagnostics require a relative /v1/... route.";
    }
  } else if (record.route != null && record.route !== "" && !isRelativeV1Route(record.route)) {
    return "Console capability routes must be relative /v1/... paths.";
  }
  return null;
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
  if (record.backend_kind !== "local" && record.backend_kind !== "hosted") {
    return { ok: false, kind: "malformed", message: "Console capabilities backend_kind invalid." };
  }
  if (record.backend_kind === "hosted") {
    const identityError = validateHostedIdentity(record.identity);
    if (identityError) {
      return { ok: false, kind: "malformed", message: identityError };
    }
  }
  if (
    !Array.isArray(record.widgets) ||
    !Array.isArray(record.diagnostics) ||
    !Array.isArray(record.controls)
  ) {
    return { ok: false, kind: "malformed", message: "Console capabilities collections malformed." };
  }
  for (const item of record.widgets) {
    const error = validateCapabilityEntry(item, true);
    if (error) {
      return { ok: false, kind: "malformed", message: error };
    }
  }
  for (const item of record.diagnostics) {
    const error = validateCapabilityEntry(item, true);
    if (error) {
      return { ok: false, kind: "malformed", message: error };
    }
  }
  for (const item of record.controls) {
    const error = validateCapabilityEntry(item, false);
    if (error) {
      return { ok: false, kind: "malformed", message: error };
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
  return item?.availability === "available" && isRelativeV1Route(item.route);
}

export function isDiagnosticAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): boolean {
  const item = findItem(capabilities?.diagnostics, id);
  return item?.availability === "available" && isRelativeV1Route(item.route);
}

export function widgetRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleWidgetId,
): string | null {
  const item = findItem(capabilities?.widgets, id);
  if (item?.availability !== "available" || !isRelativeV1Route(item.route)) {
    return null;
  }
  return item.route;
}

export function diagnosticRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): string | null {
  const item = findItem(capabilities?.diagnostics, id);
  if (item?.availability !== "available" || !isRelativeV1Route(item.route)) {
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

/** Fail-closed gate for the workspace Retry mutating control. */
export function resolveRetryCapabilityGate(options: {
  capabilities: ConsoleCapabilities | null | undefined;
  capabilitiesReady: boolean;
}): { enabled: boolean; reason: string | null } {
  if (!options.capabilitiesReady) {
    return { enabled: false, reason: "waiting for console capabilities" };
  }
  if (!options.capabilities) {
    return { enabled: false, reason: "console capabilities unavailable" };
  }
  if (!isControlAvailable(options.capabilities, "retry")) {
    return {
      enabled: false,
      reason:
        controlUnsupportedReason(options.capabilities, "retry") ?? "unsupported by backend",
    };
  }
  return { enabled: true, reason: null };
}

/**
 * Shared fail-closed negotiation for workspace log listing/tails and live
 * stream. Used by both the inspector detail path and fullscreen log columns
 * so unsupported diagnostics never issue `/logs` or `/stream`.
 */
export function resolveWorkspaceLogStreamAccess(
  capabilities: ConsoleCapabilities | null | undefined,
): { allowLogs: boolean; allowStream: boolean } {
  if (!capabilities) {
    return { allowLogs: false, allowStream: false };
  }
  return {
    allowLogs: isDiagnosticAvailable(capabilities, "workspace_logs"),
    allowStream: isDiagnosticAvailable(capabilities, "workspace_stream"),
  };
}

/** Convert absolute /v1/... capability route to console BFF path (/api/awf/...). */
export function capabilityRouteToAwfPath(route: string): string {
  if (route.startsWith("/v1/")) {
    return `/api/awf/${route.slice("/v1/".length)}`;
  }
  return route;
}

/** Resolve a templated capability route with a concrete workspace id. */
export function resolveCapabilityWorkspaceRoute(
  route: string,
  workspaceId: string,
): string {
  return route.replaceAll("{workspace_id}", encodeURIComponent(workspaceId));
}
