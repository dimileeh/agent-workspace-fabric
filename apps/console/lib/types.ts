export type WorkspaceStatus =
  | "requested"
  | "provisioning"
  | "ready"
  | "running"
  | "validating"
  | "pushing"
  | "monitoring_pr"
  | "completed"
  | "failed"
  | "cancelled"
  | "destroying"
  | "destroyed";

export type AgentRuntime = "codex" | "claude_code" | "gemini";

export type ApiEnvelope<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string; errorCode?: string; detail?: unknown };

export interface WorkspaceEvent {
  id: string;
  workspace_id: string;
  event_type: string;
  old_state: string | null;
  new_state: string | null;
  reason_code: string | null;
  payload: Record<string, unknown> | null;
  occurred_at: string;
}

export interface WorkspaceOverview {
  workspace_id: string;
  task_id: string;
  title: string;
  repo_url: string;
  base_branch: string;
  branch_name: string | null;
  agent: AgentRuntime;
  status: WorkspaceStatus;
  current_phase: string;
  active_operation: string | null;
  last_event: WorkspaceEvent | null;
  pr_url: string | null;
  failure_reason: string | null;
  failure_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface Workspace {
  id: string;
  status: WorkspaceStatus;
  version: number;
  repo_url: string;
  branch_base: string;
  branch_name: string | null;
  base_commit: string | null;
  task_title: string;
  task_prompt: string;
  task_external_id: string | null;
  agent: AgentRuntime;
  env_profile: string | null;
  profile_ref: string | null;
  requested_profile: Record<string, unknown> | null;
  resolved_profile: Record<string, unknown> | null;
  test_commands: string[];
  requires_database: boolean;
  node_id: string | null;
  compose_project_name: string | null;
  pr_url: string | null;
  failure_reason: string | null;
  failure_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RuntimeService {
  name: string;
  container_id: string | null;
  image: string | null;
  state: string;
  status: string | null;
  health: string | null;
  ports: string[];
  started_at: string | null;
}

export interface WorkspaceRuntime {
  workspace_id: string;
  compose_project_name: string | null;
  stack_state: string;
  services: RuntimeService[];
  logs_available: boolean;
  control_available: boolean;
  reason: string | null;
}

export interface WorkspaceLogStream {
  stream_id: string;
  source: string;
  name: string;
  kind: string;
  path: string;
  byte_count: number;
  line_count: number;
  opened_at: string;
  closed_at: string | null;
}

export interface WorkspaceLogRead {
  stream_id: string;
  offset: number;
  next_offset: number;
  eof: boolean;
  data: string;
}

export interface Operation {
  id: string;
  workspace_id: string;
  type: string;
  status: string;
  error_code: string | null;
  error_message: string | null;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  idempotency_key: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ListEnvelope<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export type AwfStreamFrame =
  | { type: "snapshot"; workspace: Workspace }
  | { type: "event"; event: WorkspaceEvent }
  | {
      type: "log";
      seq: number;
      workspace_id: string;
      stream_id: string;
      source: string;
      fd: string;
      offset: number;
      next_offset?: number;
      data: string;
      occurred_at?: string;
    }
  | { type: "heartbeat"; workspace_id: string }
  | { type: "error"; error_code: string; message: string };
