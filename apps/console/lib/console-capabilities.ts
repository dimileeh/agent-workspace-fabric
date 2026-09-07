import type {
  ConsoleCapabilities,
  ConsoleCapabilityItem,
  ConsoleControlId,
  ConsoleDiagnosticId,
  ConsoleWidgetId,
  WorkspaceOperatorAction,
} from "./types.ts";
import { awfPath } from "./console-urls.ts";

export const CONSOLE_SCHEMA_VERSION = 1;

/** Bounded v1 widget IDs from the console backend contract. */
export const KNOWN_WIDGET_IDS = [
  "fleet_summary",
  "resource_capacity",
  "cloud_runtime",
  "telemetry",
  "allocation",
  "cost",
] as const;

/** Exact routes for widgets that may be advertised as available. */
export const KNOWN_WIDGET_ROUTES: Readonly<Record<string, string>> = {
  fleet_summary: "/v1/console/dashboard-summary",
  resource_capacity: "/v1/metrics/resources/saturation",
  cloud_runtime: "/v1/console/cloud-runtime",
};

/** Bounded v1 diagnostic IDs and exact route templates. */
export const KNOWN_DIAGNOSTIC_ROUTES: Readonly<Record<string, string>> = {
  reliability: "/v1/metrics/workspaces/summary",
  merge_queue: "/v1/merge-queue",
  failures: "/v1/metrics/failures/summary",
  workspace_runtime: "/v1/workspaces/{workspace_id}/runtime",
  workspace_events: "/v1/workspaces/{workspace_id}/events",
  workspace_operations: "/v1/workspaces/{workspace_id}/operations",
  workspace_logs: "/v1/workspaces/{workspace_id}/logs",
  workspace_stream: "/v1/workspaces/{workspace_id}/stream",
};

export const KNOWN_DIAGNOSTIC_IDS = Object.keys(KNOWN_DIAGNOSTIC_ROUTES);

/** Bounded v1 control IDs (available controls omit route). */
export const KNOWN_CONTROL_IDS = [
  "remonitor",
  "refresh",
  "revalidate",
  "cancel",
  "retry",
] as const;

/** Bounded v1 unsupported reason codes from the console backend contract. */
export const KNOWN_UNSUPPORTED_REASON_CODES = [
  "backend_kind_local",
  "backend_kind_hosted",
  "not_implemented",
  "policy_disabled",
] as const;

export type CapabilityParseResult =
  | { ok: true; capabilities: ConsoleCapabilities; identityKey: string }
  | {
      ok: false;
      kind:
        | "missing"
        | "malformed"
        | "identity_malformed"
        | "unknown_version"
        | "auth_denied"
        | "outage";
      message: string;
      status?: number;
      /**
       * Present only when identity validated independently of inventory fields
       * (`generated_at`, widgets/diagnostics/controls). Callers preserve nav only
       * when this matches the prior trusted key.
       */
      trustedIdentityKey?: string;
    };

/** How loadCapabilities should clear after a capability parse failure. */
export type CapabilityParseFailureClear = "clear_authorized" | "clear_gated";

/**
 * Preserve legacy-safe overview nav only when a prior identity exists and the
 * failed payload still carries the same trusted identity. Otherwise advance the
 * feed epoch so late prior-tenant overview rows cannot repopulate the console.
 */
export function resolveCapabilityParseFailureClear(options: {
  priorIdentityKey: string | null;
  trustedIdentityKey?: string;
}): CapabilityParseFailureClear {
  const { priorIdentityKey, trustedIdentityKey } = options;
  if (
    priorIdentityKey !== null &&
    trustedIdentityKey !== undefined &&
    trustedIdentityKey === priorIdentityKey
  ) {
    return "clear_gated";
  }
  if (priorIdentityKey !== null) {
    return "clear_authorized";
  }
  return "clear_gated";
}

export function capabilityIdentityKey(capabilities: ConsoleCapabilities): string {
  const identity = capabilities.identity;
  const backendId = identity?.backend_id ?? "";
  const scope = identity?.scope ?? "";
  const tenantId = typeof identity?.tenant_id === "string" ? identity.tenant_id : "";
  if (capabilities.backend_kind === "hosted") {
    // Never emit a shared incomplete key — that collapses every tenant onto one
    // epoch. Parse rejects incomplete hosted identity; this is defense in depth
    // for any direct callers and for race windows around tenant switches.
    // JSON-encode fields so `|` (or other delimiters) inside values cannot
    // collide distinct (backend_id, scope, tenant_id) tuples onto one key.
    if (!backendId || !scope || tenantId.trim() === "") {
      return JSON.stringify([
        "hosted",
        "missing-tenant-discriminator",
        backendId,
        scope,
        tenantId,
      ]);
    }
    return JSON.stringify(["hosted", backendId, scope, tenantId]);
  }
  return JSON.stringify([capabilities.backend_kind, backendId, scope, tenantId]);
}

/**
 * Stable ID-keyed projection of a capability collection so negotiation equality
 * ignores provider serialization order (and per-item JSON key insertion order).
 */
function canonicalizeCapabilityCollection(
  items: ConsoleCapabilityItem[] | undefined,
): Record<
  string,
  {
    availability: string;
    semantics: string;
    route: string | null;
    reason_code: string | null;
    message: string | null;
  }
> {
  const byId: Record<
    string,
    {
      availability: string;
      semantics: string;
      route: string | null;
      reason_code: string | null;
      message: string | null;
    }
  > = {};
  for (const item of items ?? []) {
    byId[item.id] = {
      availability: item.availability,
      semantics: item.semantics,
      route: item.route ?? null,
      reason_code: item.reason_code ?? null,
      message: item.message ?? null,
    };
  }
  return Object.fromEntries(
    Object.entries(byId).sort(([left], [right]) => left.localeCompare(right)),
  );
}

/**
 * Stable projection of identity so negotiation equality ignores JSON key
 * insertion order. Field extraction matches `capabilityIdentityKey`.
 */
function canonicalizeIdentity(
  identity: ConsoleCapabilities["identity"] | null | undefined,
): { backend_id: string; scope: string; tenant_id: string } | null {
  if (identity == null) {
    return null;
  }
  return {
    backend_id: identity.backend_id ?? "",
    scope: identity.scope ?? "",
    tenant_id: typeof identity.tenant_id === "string" ? identity.tenant_id : "",
  };
}

/**
 * True when two negotiated payloads advertise the same identity and inventory.
 * `generated_at` is intentionally ignored so capability polls that only refresh
 * the timestamp can keep the prior object referentially stable (avoid restarting
 * dashboard/SSE effects that depend on `capabilities`). Collections are compared
 * as ID-keyed maps so reshuffled array order does not count as a new negotiation.
 * Identity is canonicalized so differing JSON key order does not either.
 */
export function sameCapabilityNegotiation(
  previous: ConsoleCapabilities,
  next: ConsoleCapabilities,
): boolean {
  if (previous === next) {
    return true;
  }
  if (
    previous.schema_version !== next.schema_version ||
    previous.backend_kind !== next.backend_kind ||
    capabilityIdentityKey(previous) !== capabilityIdentityKey(next)
  ) {
    return false;
  }
  return (
    JSON.stringify({
      identity: canonicalizeIdentity(previous.identity),
      widgets: canonicalizeCapabilityCollection(previous.widgets),
      diagnostics: canonicalizeCapabilityCollection(previous.diagnostics),
      controls: canonicalizeCapabilityCollection(previous.controls),
    }) ===
    JSON.stringify({
      identity: canonicalizeIdentity(next.identity),
      widgets: canonicalizeCapabilityCollection(next.widgets),
      diagnostics: canonicalizeCapabilityCollection(next.diagnostics),
      controls: canonicalizeCapabilityCollection(next.controls),
    })
  );
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

/** Matches Python tenant_id_must_not_be_blank / OpenAPI pattern .*\\S.* */
function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

function validateIdentityTenantId(identity: Record<string, unknown>): string | null {
  if (!("tenant_id" in identity) || identity.tenant_id == null) {
    return null;
  }
  if (!isNonBlankString(identity.tenant_id)) {
    return "identity.tenant_id must be a nonempty (non-whitespace) string when provided.";
  }
  return null;
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
  if (!isNonBlankString(record.tenant_id)) {
    return "Hosted console capabilities require a non-empty identity.tenant_id.";
  }
  return null;
}

function validateOptionalLocalIdentity(identity: unknown): string | null {
  if (identity == null) {
    return null;
  }
  if (typeof identity !== "object" || Array.isArray(identity)) {
    return "Console capabilities identity malformed.";
  }
  const record = identity as Record<string, unknown>;
  // When local identity is present, match Python ConsoleCapabilitiesIdentityResponse:
  // backend_id and scope are required nonempty strings (tenant_id remains optional).
  if (!isNonEmptyString(record.backend_id)) {
    return "Console capabilities identity requires a non-empty backend_id when provided.";
  }
  if (!isNonEmptyString(record.scope)) {
    return "Console capabilities identity requires a non-empty scope when provided.";
  }
  return validateIdentityTenantId(record);
}

/**
 * OpenAPI `format: date-time` / RFC 3339 profile: full date-time with `T` and a
 * timezone (`Z` or ±HH:mm). Rejects Date.parse-permissive forms like
 * `09/07/2026` or date-only `2026-09-07` that would otherwise enable controls.
 * Capturing groups let us reject impossible calendar values that Date.parse
 * would normalize (e.g. 2026-02-29 → March 1).
 */
const RFC3339_DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-](\d{2}):(\d{2}))$/;

/** True only for finite RFC 3339 date-time strings (rejects "", "not-a-date", slash dates, etc.). */
function isFiniteTimestampString(value: string): boolean {
  const match = RFC3339_DATE_TIME.exec(value);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  // Date.UTC normalizes overflow; require a round-trip to the same components so
  // impossible dates/times (2026-02-29, 25:00:00, month 13) are rejected.
  const dt = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() !== month - 1 ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return false;
  }
  if (match[8] !== "Z") {
    const tzHour = Number(match[9]);
    const tzMinute = Number(match[10]);
    if (tzHour > 23 || tzMinute > 59) {
      return false;
    }
  }
  return Number.isFinite(Date.parse(value));
}

function isRelativeV1Route(route: unknown): route is string {
  return typeof route === "string" && route.startsWith("/v1/") && !route.includes("://");
}

function routeMatchesInventory(route: string, expected: string): boolean {
  return route === expected;
}

type CapabilityCollectionKind = "widget" | "diagnostic" | "control";

function knownIdsFor(kind: CapabilityCollectionKind): ReadonlySet<string> {
  if (kind === "widget") {
    return new Set<string>(KNOWN_WIDGET_IDS);
  }
  if (kind === "diagnostic") {
    return new Set<string>(KNOWN_DIAGNOSTIC_IDS);
  }
  return new Set<string>(KNOWN_CONTROL_IDS);
}

function expectedRouteFor(kind: CapabilityCollectionKind, id: string): string | null {
  if (kind === "widget") {
    return KNOWN_WIDGET_ROUTES[id] ?? null;
  }
  if (kind === "diagnostic") {
    return KNOWN_DIAGNOSTIC_ROUTES[id] ?? null;
  }
  return null;
}

function validateCapabilityEntry(
  item: unknown,
  requireRouteWhenAvailable: boolean,
  kind: CapabilityCollectionKind,
  seenIds: Set<string>,
): string | null {
  if (item == null || typeof item !== "object" || Array.isArray(item)) {
    return "Console capability entry malformed.";
  }
  const record = item as Record<string, unknown>;
  if (typeof record.id !== "string" || record.id.length === 0) {
    return "Console capability entry missing id.";
  }
  if (!knownIdsFor(kind).has(record.id)) {
    return `Unknown console ${kind} id=${record.id}.`;
  }
  if (seenIds.has(record.id)) {
    return `Duplicate console ${kind} id=${record.id}.`;
  }
  seenIds.add(record.id);
  if (record.availability !== "available" && record.availability !== "unsupported") {
    return "Console capability availability invalid.";
  }
  if (typeof record.semantics !== "string" || record.semantics.length === 0) {
    return "Console capability entry missing semantics.";
  }

  if (record.availability === "unsupported") {
    if (!isNonEmptyString(record.reason_code)) {
      return "Unsupported console capability entry requires a non-empty reason_code.";
    }
    if (
      !(KNOWN_UNSUPPORTED_REASON_CODES as readonly string[]).includes(record.reason_code)
    ) {
      return `Unsupported console capability reason_code=${record.reason_code} is outside the v1 inventory.`;
    }
    if (!isNonEmptyString(record.message)) {
      return "Unsupported console capability entry requires a non-empty message.";
    }
  }

  const expectedRoute = expectedRouteFor(kind, record.id);
  if (record.availability === "available" && requireRouteWhenAvailable) {
    if (!isRelativeV1Route(record.route)) {
      return "Available console widgets/diagnostics require a relative /v1/... route.";
    }
    if (expectedRoute == null) {
      return `Available console ${kind} id=${record.id} has no inventory route.`;
    }
    if (!routeMatchesInventory(record.route, expectedRoute)) {
      return `Console ${kind} id=${record.id} route must be ${expectedRoute}.`;
    }
  } else if (record.route != null && record.route !== "") {
    if (!isRelativeV1Route(record.route)) {
      return "Console capability routes must be relative /v1/... paths.";
    }
    if (expectedRoute != null && !routeMatchesInventory(record.route, expectedRoute)) {
      return `Console ${kind} id=${record.id} route must be ${expectedRoute}.`;
    }
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
  // Identity is authoritative for feed-epoch decisions and must be extracted
  // before optional inventory fields (`generated_at`, collections). A payload
  // that fails inventory checks can still carry a trusted identity change.
  if (record.backend_kind === "hosted") {
    const identityError = validateHostedIdentity(record.identity);
    if (identityError) {
      return { ok: false, kind: "identity_malformed", message: identityError };
    }
  } else {
    const identityError = validateOptionalLocalIdentity(record.identity);
    if (identityError) {
      return { ok: false, kind: "identity_malformed", message: identityError };
    }
  }
  const trustedIdentityKey = capabilityIdentityKey({
    schema_version: CONSOLE_SCHEMA_VERSION,
    backend_kind: record.backend_kind,
    generated_at: "",
    identity: record.identity as ConsoleCapabilities["identity"],
    widgets: [],
    diagnostics: [],
    controls: [],
  });
  if (
    typeof record.generated_at !== "string" ||
    !isFiniteTimestampString(record.generated_at)
  ) {
    return {
      ok: false,
      kind: "malformed",
      message: "Console capabilities generated_at must be a finite ISO timestamp.",
      trustedIdentityKey,
    };
  }
  if (
    !Array.isArray(record.widgets) ||
    !Array.isArray(record.diagnostics) ||
    !Array.isArray(record.controls)
  ) {
    return {
      ok: false,
      kind: "malformed",
      message: "Console capabilities collections malformed.",
      trustedIdentityKey,
    };
  }
  const seenWidgets = new Set<string>();
  for (const item of record.widgets) {
    const error = validateCapabilityEntry(item, true, "widget", seenWidgets);
    if (error) {
      return { ok: false, kind: "malformed", message: error, trustedIdentityKey };
    }
  }
  const seenDiagnostics = new Set<string>();
  for (const item of record.diagnostics) {
    const error = validateCapabilityEntry(item, true, "diagnostic", seenDiagnostics);
    if (error) {
      return { ok: false, kind: "malformed", message: error, trustedIdentityKey };
    }
  }
  const seenControls = new Set<string>();
  for (const item of record.controls) {
    const error = validateCapabilityEntry(item, false, "control", seenControls);
    if (error) {
      return { ok: false, kind: "malformed", message: error, trustedIdentityKey };
    }
  }
  const capabilities = payload as ConsoleCapabilities;
  return {
    ok: true,
    capabilities,
    identityKey: trustedIdentityKey,
  };
}

function findItem(
  items: ConsoleCapabilityItem[] | undefined,
  id: string,
): ConsoleCapabilityItem | undefined {
  return items?.find((item) => item.id === id);
}

function inventoryRouteMatches(
  kind: "widget" | "diagnostic",
  id: string,
  route: string | null | undefined,
): boolean {
  if (!isRelativeV1Route(route)) {
    return false;
  }
  const expected = expectedRouteFor(kind, id);
  return expected != null && routeMatchesInventory(route, expected);
}

export function isWidgetAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleWidgetId,
): boolean {
  const item = findItem(capabilities?.widgets, id);
  return item?.availability === "available" && inventoryRouteMatches("widget", id, item.route);
}

export function isDiagnosticAvailable(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): boolean {
  const item = findItem(capabilities?.diagnostics, id);
  return (
    item?.availability === "available" && inventoryRouteMatches("diagnostic", id, item.route)
  );
}

export function widgetRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleWidgetId,
): string | null {
  const item = findItem(capabilities?.widgets, id);
  if (item?.availability !== "available" || !inventoryRouteMatches("widget", id, item.route)) {
    return null;
  }
  return item.route ?? null;
}

export function diagnosticRoute(
  capabilities: ConsoleCapabilities | null | undefined,
  id: ConsoleDiagnosticId,
): string | null {
  const item = findItem(capabilities?.diagnostics, id);
  if (
    item?.availability !== "available" ||
    !inventoryRouteMatches("diagnostic", id, item.route)
  ) {
    return null;
  }
  return item.route ?? null;
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

/**
 * Inventory used for mutating operator controls (retry/cancel/remonitor/…).
 * Read-only feeds may retain last-good capabilities across a capability-endpoint
 * outage, but mutation stays fail-closed until negotiation succeeds again.
 */
export function capabilitiesForMutatingControls(
  capabilities: ConsoleCapabilities | null | undefined,
  capabilityError: string | null | undefined,
): ConsoleCapabilities | null {
  if (capabilityError) {
    return null;
  }
  return capabilities ?? null;
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
): { allowLogs: boolean; allowStream: boolean; allowStreamLogs: boolean } {
  if (!capabilities) {
    return { allowLogs: false, allowStream: false, allowStreamLogs: false };
  }
  const allowLogs = isDiagnosticAvailable(capabilities, "workspace_logs");
  const allowStream = isDiagnosticAvailable(capabilities, "workspace_stream");
  // Live log frames need the listing UI (stream picker / fullscreen columns).
  // Keep allowStream for events/workspace snapshots when logs are unsupported.
  return {
    allowLogs,
    allowStream,
    allowStreamLogs: allowLogs && allowStream,
  };
}

/**
 * Convert absolute /v1/... capability route through the configured console API
 * base (`awfPath`), including hosted `/api/core-console` and context query carry.
 * Optional `pageSearch` is a test seam; browsers read `window.location.search`.
 */
export function capabilityRouteToAwfPath(route: string, pageSearch?: string): string {
  if (route.startsWith("/v1/")) {
    return awfPath(route.slice("/v1/".length), undefined, pageSearch);
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
