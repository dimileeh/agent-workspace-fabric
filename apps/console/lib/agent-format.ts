import type { WorkspaceOverview } from "@/lib/types";

type AgentLabelWorkspace = Pick<WorkspaceOverview, "agent" | "agent_model" | "agent_effort">;
type AgentTitleWorkspace = Pick<WorkspaceOverview, "agent" | "agent_model" | "agent_effort"> &
  Partial<Pick<WorkspaceOverview, "agent_model_source" | "agent_effort_source">>;
type AgentEffortWorkspace = Pick<WorkspaceOverview, "agent_effort"> &
  Partial<Pick<WorkspaceOverview, "agent_effort_source">>;

export function formatAgentLabel(workspace: AgentLabelWorkspace): string {
  const model = compactAgentModel(workspace.agent_model);
  return [workspace.agent, model, workspace.agent_effort].filter(Boolean).join(" · ");
}

export function formatAgentTitle(workspace: AgentTitleWorkspace): string {
  const parts: string[] = [workspace.agent];
  if (workspace.agent_model) {
    parts.push(workspace.agent_model);
  }
  if (workspace.agent_effort) {
    parts.push(`effort ${workspace.agent_effort}`);
  }
  if (workspace.agent_model_source && workspace.agent_model_source !== "default") {
    parts.push(`model ${workspace.agent_model_source}`);
  }
  if (workspace.agent_effort_source && workspace.agent_effort_source !== "default") {
    parts.push(`effort ${workspace.agent_effort_source}`);
  }
  return parts.join(" / ");
}

export function formatAgentEffort(workspace: AgentEffortWorkspace): string {
  if (!workspace.agent_effort) {
    return "—";
  }
  return workspace.agent_effort_source
    ? `${workspace.agent_effort} (${workspace.agent_effort_source})`
    : workspace.agent_effort;
}

export function compactAgentModel(model: string | null | undefined): string | null {
  if (!model) {
    return null;
  }
  return model.startsWith("ollama/") ? model.slice("ollama/".length) : model;
}
