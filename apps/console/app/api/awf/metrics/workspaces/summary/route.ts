import { proxyAwfGet } from "@/lib/awf-server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const search = new URL(request.url).search;
  return proxyAwfGet(`/v1/metrics/workspaces/summary${search}`);
}
