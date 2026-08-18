/**
 * Single config/URL builder boundary for local and hosted console deployments.
 *
 * Local defaults (unset env):
 *   basePath "" | apiBase "/api/awf" | operatorBase "/api/operator"
 *   contextQueryKeys []
 *
 * Hosted example:
 *   NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=/workspaces
 *   NEXT_PUBLIC_AWF_CONSOLE_API_BASE=/api/core-console
 *   NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=/api/core-console
 *   NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS=org_id,project_id
 *
 * Next.js basePath does not rewrite absolute client fetch/EventSource paths —
 * every browser-visible AWF/operator URL must go through these helpers.
 * Context keys are merged into awfPath/operatorPath only (never consoleHref).
 */

export type ConsoleUrlConfig = {
  basePath: string;
  apiBase: string;
  operatorBase: string;
  contextQueryKeys: string[];
};

export type ConsoleUrlQuery = Record<string, string | number | boolean | null | undefined>;

const DEFAULT_API_BASE = "/api/awf";
const DEFAULT_OPERATOR_BASE = "/api/operator";

/** Max accepted context key name length (fail closed beyond). */
const MAX_CONTEXT_KEY_LENGTH = 64;
/** Max accepted configured context keys (fail closed beyond). */
const MAX_CONTEXT_KEY_COUNT = 8;

const SAFE_CONTEXT_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Case-insensitive substrings that must never be configured as context keys. */
const FORBIDDEN_CONTEXT_KEY_SUBSTRINGS = [
  "token",
  "secret",
  "password",
  "api_key",
  "authorization",
  "cookie",
  "credential",
] as const;

type EnvLike = Record<string, string | undefined>;

export function getConsoleUrlConfig(overrides?: EnvLike): ConsoleUrlConfig {
  // Read NEXT_PUBLIC_* via direct process.env member access so Next can inline
  // them into the client bundle. Dynamic env lookups are not replaced at build time.
  return {
    basePath: normalizeBasePath(
      overrides?.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH ?? process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH,
    ),
    apiBase: normalizeServiceBase(
      overrides?.NEXT_PUBLIC_AWF_CONSOLE_API_BASE ?? process.env.NEXT_PUBLIC_AWF_CONSOLE_API_BASE,
      DEFAULT_API_BASE,
    ),
    operatorBase: normalizeServiceBase(
      overrides?.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE ??
        process.env.NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE,
      DEFAULT_OPERATOR_BASE,
    ),
    contextQueryKeys: parseContextQueryKeys(
      overrides?.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS ??
        process.env.NEXT_PUBLIC_AWF_CONSOLE_CONTEXT_QUERY_KEYS,
    ),
  };
}

/**
 * Parse build-time context query key names. Deterministic and fail-closed:
 * invalid, oversized, or credential-like names are omitted (never thrown).
 */
export function parseContextQueryKeys(raw: string | undefined): string[] {
  if (raw === undefined || raw === null) {
    return [];
  }
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const segment of raw.split(",")) {
    if (keys.length >= MAX_CONTEXT_KEY_COUNT) {
      break;
    }
    const key = segment.trim();
    if (!key || seen.has(key)) {
      continue;
    }
    if (!isSafeContextQueryKey(key)) {
      continue;
    }
    seen.add(key);
    keys.push(key);
  }
  return keys;
}

function isSafeContextQueryKey(key: string): boolean {
  if (key.length > MAX_CONTEXT_KEY_LENGTH) {
    return false;
  }
  if (!SAFE_CONTEXT_KEY.test(key)) {
    return false;
  }
  const lower = key.toLowerCase();
  for (const forbidden of FORBIDDEN_CONTEXT_KEY_SUBSTRINGS) {
    if (lower.includes(forbidden)) {
      return false;
    }
  }
  return true;
}

/**
 * Join the configured AWF API base with a relative path and optional query.
 * When context query keys are configured, copies missing keys from the current
 * page search (or the optional `pageSearch` seam for tests).
 */
export function awfPath(
  relativePath: string,
  query?: ConsoleUrlQuery | URLSearchParams,
  pageSearch?: string,
): string {
  const config = getConsoleUrlConfig();
  return withQuery(
    joinBase(config.apiBase, relativePath),
    mergeContextFromPage(query, config.contextQueryKeys, pageSearch),
  );
}

/**
 * Join the configured operator API base with a relative path and optional query.
 * Applies the same bounded context-key carry as `awfPath`.
 */
export function operatorPath(
  relativePath: string,
  query?: ConsoleUrlQuery | URLSearchParams,
  pageSearch?: string,
): string {
  const config = getConsoleUrlConfig();
  return withQuery(
    joinBase(config.operatorBase, relativePath),
    mergeContextFromPage(query, config.contextQueryKeys, pageSearch),
  );
}

/**
 * App-relative href under the configured Next basePath.
 * Prefer Next `<Link>` for in-app navigation; use this for absolute browser paths
 * (e.g. window.location) that must include basePath.
 * Never auto-injects configured context query keys.
 */
export function consoleHref(relativePath: string, query?: ConsoleUrlQuery | URLSearchParams): string {
  const { basePath } = getConsoleUrlConfig();
  const joined = basePath
    ? joinBase(basePath, relativePath || "/")
    : normalizeLeadingSlash(relativePath || "/");
  return withQuery(joined, query);
}

export function normalizeBasePath(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed || trimmed === "/") {
    return "";
  }
  return stripTrailingSlash(normalizeLeadingSlash(trimmed));
}

export function normalizeServiceBase(value: string | undefined, fallback: string): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed) {
    return stripTrailingSlash(normalizeLeadingSlash(fallback));
  }
  return stripTrailingSlash(normalizeLeadingSlash(trimmed));
}

function mergeContextFromPage(
  query: ConsoleUrlQuery | URLSearchParams | undefined,
  contextKeys: string[],
  pageSearch?: string,
): ConsoleUrlQuery | URLSearchParams | undefined {
  if (contextKeys.length === 0) {
    return query;
  }
  const params =
    query instanceof URLSearchParams
      ? new URLSearchParams(query)
      : query
        ? toSearchParams(query)
        : new URLSearchParams();

  const pageParams = resolvePageSearchParams(pageSearch);
  if (!pageParams) {
    return params.toString() ? params : query;
  }

  for (const key of contextKeys) {
    if (params.has(key)) {
      continue;
    }
    const value = pageParams.get(key);
    if (value === null || value === "") {
      continue;
    }
    params.set(key, value);
  }

  return params.toString() ? params : query;
}

function resolvePageSearchParams(pageSearch?: string): URLSearchParams | null {
  if (pageSearch !== undefined) {
    const raw = pageSearch.startsWith("?") ? pageSearch.slice(1) : pageSearch;
    return new URLSearchParams(raw);
  }
  if (typeof window === "undefined") {
    return null;
  }
  return new URLSearchParams(window.location.search);
}

function joinBase(base: string, relativePath: string): string {
  const normalizedBase = stripTrailingSlash(normalizeLeadingSlash(base));
  const cleanedRelative = relativePath.replace(/^\/+/, "").replace(/\/+$/, "");
  if (!cleanedRelative) {
    return normalizedBase || "/";
  }
  return `${normalizedBase}/${cleanedRelative}`;
}

function withQuery(path: string, query?: ConsoleUrlQuery | URLSearchParams): string {
  if (!query) {
    return path;
  }
  const params =
    query instanceof URLSearchParams ? query : toSearchParams(query);
  const encoded = params.toString();
  return encoded ? `${path}?${encoded}` : path;
}

function toSearchParams(query: ConsoleUrlQuery): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === false) {
      continue;
    }
    if (value === true) {
      params.set(key, "true");
      continue;
    }
    params.set(key, String(value));
  }
  return params;
}

function normalizeLeadingSlash(value: string): string {
  return value.startsWith("/") ? value : `/${value}`;
}

function stripTrailingSlash(value: string): string {
  if (value.length <= 1) {
    return value;
  }
  return value.replace(/\/+$/, "");
}
