import { proxyAwfGet } from "@/lib/awf-server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  return proxyAwfGet("/v1/metrics/failures/summary");
}
