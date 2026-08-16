/**
 * Single config/URL builder boundary for local and hosted console deployments.
 *
 * Local defaults (unset env):
 *   basePath "" | apiBase "/api/awf" | operatorBase "/api/operator"
 *
 * Hosted example:
 *   NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=/workspaces
 *   NEXT_PUBLIC_AWF_CONSOLE_API_BASE=/workspaces/api/awf
 *   NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=/workspaces/api/operator
 *
 * Next.js basePath does not rewrite absolute client fetch/EventSource paths —
 * every browser-visible AWF/operator URL must go through these helpers.
 */

export type ConsoleUrlConfig = {
  basePath: string;
  apiBase: string;
  operatorBase: string;
};

export type ConsoleUrlQuery = Record<string, string | number | boolean | null | undefined>;

const DEFAULT_API_BASE = "/api/awf";
const DEFAULT_OPERATOR_BASE = "/api/operator";

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
  };
}

/** Join the configured AWF API base with a relative path and optional query. */
export function awfPath(relativePath: string, query?: ConsoleUrlQuery | URLSearchParams): string {
  return withQuery(joinBase(getConsoleUrlConfig().apiBase, relativePath), query);
}

/** Join the configured operator API base with a relative path and optional query. */
export function operatorPath(
  relativePath: string,
  query?: ConsoleUrlQuery | URLSearchParams,
): string {
  return withQuery(joinBase(getConsoleUrlConfig().operatorBase, relativePath), query);
}

/**
 * App-relative href under the configured Next basePath.
 * Prefer Next `<Link>` for in-app navigation; use this for absolute browser paths
 * (e.g. window.location) that must include basePath.
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
