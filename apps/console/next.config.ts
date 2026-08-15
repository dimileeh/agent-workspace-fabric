import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const defaultAllowedDevOrigins = ["127.0.0.1", "localhost"];
const allowedDevOrigins = uniqueAllowedDevOrigins([
  ...defaultAllowedDevOrigins,
  ...parseAllowedDevOrigins(process.env.AWF_CONSOLE_ALLOWED_DEV_ORIGINS),
]);
// Keep in sync with normalizeBasePath in lib/console-urls.ts (avoid importing
// that module here so the slim runtime image does not need apps/console/lib).
const basePath = normalizeBasePath(process.env.NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Allow a second Playwright-hosted dev server from the same tree.
  distDir: process.env.AWF_CONSOLE_DIST_DIR || ".next",
  turbopack: {
    root,
  },
  ...(basePath ? { basePath } : {}),
  ...(allowedDevOrigins.length > 0 ? { allowedDevOrigins } : {}),
};

export default nextConfig;

function normalizeBasePath(value: string | undefined): string {
  const trimmed = (value ?? "").trim();
  if (!trimmed || trimmed === "/") {
    return "";
  }
  const withLeading = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeading.replace(/\/+$/, "");
}

function parseAllowedDevOrigins(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);
}

function uniqueAllowedDevOrigins(origins: string[]): string[] {
  return Array.from(new Set(origins));
}
