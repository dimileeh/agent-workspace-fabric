const validateUrl = process.env.BROWSER_VALIDATE_URL;
if (!validateUrl) {
  throw new Error("BROWSER_VALIDATE_URL is required");
}

const response = await fetch(validateUrl);
const body = await response.text();
if (!response.ok || body !== "browser validated awf-node-profile-fixture\n") {
  throw new Error(`unexpected browser validation response: ${response.status} ${JSON.stringify(body)}`);
}

process.stdout.write(body);
