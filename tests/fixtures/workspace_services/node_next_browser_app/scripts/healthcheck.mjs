const target = process.argv[2];
const DEFAULT_FETCH_TIMEOUT_MS = 5000;

function fetchTimeoutMs() {
  const raw = process.env.AWF_HEALTHCHECK_FETCH_TIMEOUT_MS ?? String(DEFAULT_FETCH_TIMEOUT_MS);
  const timeoutMs = Number(raw);
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error("AWF_HEALTHCHECK_FETCH_TIMEOUT_MS must be a positive integer");
  }
  return timeoutMs;
}

function browserHealthUrl() {
  const validateUrl = process.env.BROWSER_VALIDATE_URL;
  if (!validateUrl) {
    throw new Error("BROWSER_VALIDATE_URL is required");
  }
  return new URL("/healthz", validateUrl);
}

function appHealthUrl() {
  const baseUrl = process.env.APP_BASE_URL;
  if (!baseUrl) {
    throw new Error("APP_BASE_URL is required");
  }
  return new URL("/healthz", baseUrl);
}

async function fetchHealthResponse(url, targetName) {
  const timeoutMs = fetchTimeoutMs();
  const signal = AbortSignal.timeout(timeoutMs);

  try {
    const response = await fetch(url, { signal });
    const body = await response.text();
    return { body, response };
  } catch (error) {
    if (
      signal.aborted ||
      (error instanceof Error && (error.name === "AbortError" || error.name === "TimeoutError"))
    ) {
      throw new Error(`timed out fetching ${targetName} health response after ${timeoutMs}ms`, {
        cause: error,
      });
    }
    throw error;
  }
}

const url = target === "browser" ? browserHealthUrl() : target === "app" ? appHealthUrl() : null;
if (url === null) {
  throw new Error("usage: node scripts/healthcheck.mjs app|browser");
}

const { body, response } = await fetchHealthResponse(url, target);
if (!response.ok || body.trim() !== "ok") {
  throw new Error(`unexpected ${target} health response: ${response.status} ${JSON.stringify(body)}`);
}

process.stdout.write(body);
