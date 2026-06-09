const [url, expected] = process.argv.slice(2);
if (!url || !expected) {
  throw new Error("usage: node /app/scripts/container-healthcheck.mjs <url> <expected-trimmed-body>");
}

const response = await fetch(url);
const body = await response.text();
if (!response.ok || body.trim() !== expected) {
  throw new Error(`unexpected health response: ${response.status} ${JSON.stringify(body)}`);
}

process.stdout.write("ok\n");
