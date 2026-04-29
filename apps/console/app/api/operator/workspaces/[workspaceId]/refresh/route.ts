import { handleWorkspaceControlRoute } from "@/lib/workspace-control-routes";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ workspaceId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { workspaceId } = await context.params;
  return handleWorkspaceControlRoute("refresh", workspaceId, request);
}
