import { proxyAwf } from "@/lib/awf-server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ id: string }>;
};

export async function POST(_request: Request, context: RouteContext) {
  const { id } = await context.params;
  return proxyAwf(`/v1/workspaces/${encodeURIComponent(id)}/retry`, {
    method: "POST",
  });
}
