import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const metricRoutes = [
  {
    path: "../app/api/awf/metrics/workspaces/summary/route.ts",
    upstream: "/v1/metrics/workspaces/summary",
  },
  {
    path: "../app/api/awf/metrics/failures/summary/route.ts",
    upstream: "/v1/metrics/failures/summary",
  },
  {
    path: "../app/api/awf/metrics/resources/saturation/route.ts",
    upstream: "/v1/metrics/resources/saturation",
  },
];

for (const route of metricRoutes) {
  test(`${route.path} preserves query strings`, () => {
    const source = readFileSync(new URL(route.path, import.meta.url), "utf8");

    assert.match(source, /export async function GET\(request: Request\)/);
    assert.match(source, /const search = new URL\(request\.url\)\.search;/);
    assert.ok(
      source.includes(`return proxyAwfGet(\`${route.upstream}\${search}\`);`),
      `Expected ${route.path} to forward query strings to ${route.upstream}`,
    );
  });
}

test("revalidate operator route delegates to the shared control handler", () => {
  const source = readFileSync(
    new URL("../app/api/operator/workspaces/[workspaceId]/revalidate/route.ts", import.meta.url),
    "utf8",
  );

  assert.match(source, /export async function POST\(request: Request, context: RouteContext\)/);
  assert.match(source, /const \{ workspaceId \} = await context\.params;/);
  assert.match(
    source,
    /return handleWorkspaceControlRoute\("revalidate", workspaceId, request\);/,
  );
});

test("cancel operator route delegates to the shared control handler", () => {
  const source = readFileSync(
    new URL("../app/api/operator/workspaces/[workspaceId]/cancel/route.ts", import.meta.url),
    "utf8",
  );

  assert.match(source, /export async function POST\(request: Request, context: RouteContext\)/);
  assert.match(source, /const \{ workspaceId \} = await context\.params;/);
  assert.match(
    source,
    /return handleWorkspaceControlRoute\("cancel", workspaceId, request\);/,
  );
});
