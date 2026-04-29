const target = process.argv[2];

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

const url = target === "browser" ? browserHealthUrl() : target === "app" ? appHealthUrl() : null;
if (url === null) {
  throw new Error("usage: node scripts/healthcheck.mjs app|browser");
}

const response = await fetch(url);
const body = await response.text();
if (!response.ok || body !== "ok\n") {
  throw new Error(`unexpected ${target} health response: ${response.status} ${JSON.stringify(body)}`);
}

process.stdout.write(body);
