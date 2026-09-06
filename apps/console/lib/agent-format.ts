import type { WorkspaceOverview } from "@/lib/types";

type AgentLabelWorkspace = Pick<
  WorkspaceOverview,
  "agent" | "agent_model" | "agent_effort" | "cursor_auto_mode"
>;
type AgentTitleWorkspace = Pick<
  WorkspaceOverview,
  "agent" | "agent_model" | "agent_effort" | "cursor_auto_mode"
> &
  Partial<Pick<WorkspaceOverview, "agent_model_source" | "agent_effort_source">>;
type AgentEffortWorkspace = Pick<WorkspaceOverview, "agent_effort"> &
  Partial<Pick<WorkspaceOverview, "agent_effort_source">>;

type RequestedModelWorkspace = Partial<
  Pick<
    WorkspaceOverview,
    | "requested_model"
    | "requested_effort"
    | "requested_model_source"
    | "requested_effort_source"
    | "agent_model"
    | "agent_effort"
    | "agent_model_source"
    | "agent_effort_source"
  >
>;

type ConfirmedModelWorkspace = Partial<
  Pick<WorkspaceOverview, "confirmed_execution_model" | "confirmed_execution_model_source">
>;

const NON_CONFIRMED_SOURCES = new Set(["task_policy", "default", "auto", "unavailable"]);

export function formatAgentLabel(workspace: AgentLabelWorkspace): string {
  const model = displayAgentModel(workspace);
  return [workspace.agent, model, workspace.agent_effort].filter(Boolean).join(" · ");
}

export function formatAgentTitle(workspace: AgentTitleWorkspace): string {
  const parts: string[] = [workspace.agent];
  const model = displayAgentModel(workspace);
  if (model) {
    parts.push(model);
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

/** Sources that must never be labeled as confirmed execution evidence. */
export function isConfirmedModelSource(source: string | null | undefined): boolean {
  if (!source) {
    return false;
  }
  return !NON_CONFIRMED_SOURCES.has(source.toLowerCase());
}

export function formatRequestedModel(workspace: RequestedModelWorkspace): string {
  const model = workspace.requested_model ?? workspace.agent_model;
  if (!model) {
    return "not recorded";
  }
  const source = workspace.requested_model_source ?? workspace.agent_model_source;
  const compact = compactAgentModel(model) ?? model;
  return source ? `${compact} (${source})` : compact;
}

export function formatRequestedEffort(workspace: RequestedModelWorkspace): string {
  const effort = workspace.requested_effort ?? workspace.agent_effort;
  if (!effort) {
    return "not recorded";
  }
  const source = workspace.requested_effort_source ?? workspace.agent_effort_source;
  return source ? `${effort} (${source})` : effort;
}

/**
 * Confirmed execution model only when provenance is real execution evidence.
 * Never labels task_policy / default / auto as confirmed.
 */
export function formatConfirmedExecutionModel(workspace: ConfirmedModelWorkspace): string {
  const source = workspace.confirmed_execution_model_source;
  if (!isConfirmedModelSource(source)) {
    return "not recorded";
  }
  const model = workspace.confirmed_execution_model;
  if (!model) {
    return "not recorded";
  }
  const compact = compactAgentModel(model) ?? model;
  return `${compact} (${source})`;
}

function displayAgentModel(workspace: AgentLabelWorkspace): string | null {
  if (workspace.agent === "cursor" && workspace.cursor_auto_mode) {
    const mode = workspace.cursor_auto_mode;
    return `Auto ${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
  }
  return compactAgentModel(workspace.agent_model);
}
