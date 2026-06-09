const baseUrl = process.env.APP_BASE_URL;
if (!baseUrl) {
  throw new Error("APP_BASE_URL is required");
}

const response = await fetch(new URL("/setup", baseUrl));
const body = await response.text();
if (!response.ok || body !== "setup ok\n") {
  throw new Error(`unexpected setup response: ${response.status} ${JSON.stringify(body)}`);
}

process.stdout.write(body);
