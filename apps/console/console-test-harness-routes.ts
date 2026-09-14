/**
 * Resolves Next.js `pageExtensions` so App Router test-harness pages
 * (`page.harness.tsx`) are only part of the route graph for non-production
 * builds when AWF_CONSOLE_TEST_HARNESS=1 (Playwright webServer).
 */

export const DEFAULT_PAGE_EXTENSIONS = Object.freeze(["tsx", "ts", "jsx", "js"]);

/** Extension suffix for test-only App Router pages: `page.harness.tsx`. */
export const HARNESS_PAGE_EXTENSION = "harness.tsx";

type EnvLike = NodeJS.ProcessEnv | Record<string, string | undefined>;

export function isConsoleTestHarnessRouteBuildEnabled(env: EnvLike = process.env): boolean {
  return env.AWF_CONSOLE_TEST_HARNESS === "1" && env.NODE_ENV !== "production";
}

export function resolveConsolePageExtensions(env: EnvLike = process.env): string[] {
  if (!isConsoleTestHarnessRouteBuildEnabled(env)) {
    return [...DEFAULT_PAGE_EXTENSIONS];
  }
  return [...DEFAULT_PAGE_EXTENSIONS, HARNESS_PAGE_EXTENSION];
}
