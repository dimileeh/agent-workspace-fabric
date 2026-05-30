const validateUrl = process.env.BROWSER_VALIDATE_URL;
if (!validateUrl) {
  throw new Error("BROWSER_VALIDATE_URL is required");
}

const EXPECTED_BODY = "browser validated awf-node-profile-fixture\n";
const attempts = positiveIntegerFromEnv("BROWSER_VALIDATE_ATTEMPTS", 3);
const retryDelayMs = positiveIntegerFromEnv("BROWSER_VALIDATE_RETRY_DELAY_MS", 1000);
const fetchTimeoutMs = positiveIntegerFromEnv("BROWSER_VALIDATE_FETCH_TIMEOUT_MS", 30000);

function positiveIntegerFromEnv(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") {
    return fallback;
  }

  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value <= 0 || String(value) !== raw.trim()) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function fetchValidationBody(url) {
  const signal = AbortSignal.timeout(fetchTimeoutMs);
  let response;
  try {
    response = await fetch(url, { signal });
  } catch (error) {
    if (
      signal.aborted ||
      (error instanceof Error && (error.name === "AbortError" || error.name === "TimeoutError"))
    ) {
      throw new Error(`timed out fetching browser validation after ${fetchTimeoutMs}ms`, {
        cause: error,
      });
    }
    throw error;
  }

  const body = await response.text();
  if (!response.ok || body !== EXPECTED_BODY) {
    throw new Error(`unexpected browser validation response: ${response.status} ${JSON.stringify(body)}`);
  }
  return body;
}

let lastError;
let validatedBody;
for (let attempt = 1; attempt <= attempts; attempt += 1) {
  try {
    validatedBody = await fetchValidationBody(validateUrl);
    break;
  } catch (error) {
    lastError = error;
    if (attempt >= attempts) {
      break;
    }

    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`browser validation attempt ${attempt} failed: ${message}\n`);
    await sleep(retryDelayMs);
  }
}

if (validatedBody !== undefined) {
  process.stdout.write(validatedBody);
} else {
  throw lastError ?? new Error("browser validation did not run");
}
