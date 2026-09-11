import { handleWorkspaceRetryRoute } from "@/lib/workspace-control-routes";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ id: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { id } = await context.params;
  return handleWorkspaceRetryRoute(id, request);
}
