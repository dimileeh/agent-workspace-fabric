import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const defaultAllowedDevOrigins = ["127.0.0.1", "localhost"];
const allowedDevOrigins = uniqueAllowedDevOrigins([
  ...defaultAllowedDevOrigins,
  ...parseAllowedDevOrigins(process.env.AWF_CONSOLE_ALLOWED_DEV_ORIGINS),
]);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  turbopack: {
    root,
  },
  ...(allowedDevOrigins.length > 0 ? { allowedDevOrigins } : {}),
};

export default nextConfig;

function parseAllowedDevOrigins(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
}

function uniqueAllowedDevOrigins(origins: string[]): string[] {
  return Array.from(new Set(origins));
}
