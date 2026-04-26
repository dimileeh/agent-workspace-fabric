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

export type MergeBlockerReason =
  | "ready_to_merge_or_waiting_for_github"
  | "manual_merge_required"
  | "waiting_for_monitor"
  | "workspace_not_terminal"
  | "completed"
  | "failed_or_cancelled"
  | "not_canonical"
  | "stale";

export type MergeCandidateStatus = "open" | "merged" | "closed";

export interface MergeCandidateReadiness {
  ready: boolean;
  manual_merge_required: boolean;
  waiting_for_monitor: boolean;
  failed_or_cancelled: boolean;
  completed: boolean;
  not_canonical: boolean;
  stale: boolean;
}

export type ValidationTier = 1 | 2 | 3;

export type ValidationProvenanceStatus = "running" | "succeeded" | "failed" | "unknown";

export interface ValidationRunSummary {
  validation_run_id: string;
  attempt_id: string | null;
  tier: ValidationTier;
  command_set_hash: string;
  base_commit: string | null;
  target_branch: string | null;
  target_head_sha: string | null;
  current_target_head_sha: string | null;
  status: ValidationProvenanceStatus;
  reason_code: string | null;
  started_at: string;
  finished_at: string | null;
  log_stream_refs: Record<string, unknown>;
  fresh_for_target: boolean | null;
}

export type StaleReasonCode =
  | "STALE_TARGET_ADVANCED"
  | "STALE_OVERLAP"
  | "STALE_DEPENDENCY"
  | "STALE_BUILD_CONFIG"
  | "STALE_SCHEMA";

export type StaleReasonTrigger =
  | "target_advanced"
  | "path_overlap"
  | "schema_changed"
  | "dependency_changed"
  | "build_config_changed";

export type StaleReasonStatus = "active" | "resolved";

export interface StaleReason {
  id: string;
  workspace_id: string;
  candidate_id: string | null;
  attempt_id: string | null;
  task_id: string | null;
  trigger_type: StaleReasonTrigger;
  trigger_ref: string | null;
  reason_code: StaleReasonCode;
  explanation: string;
  status: StaleReasonStatus;
  detected_at: string;
  resolved_at: string | null;
}

export interface MergeQueueItem {
  candidate_id: string | null;
  candidate_status: MergeCandidateStatus | null;
  close_reason: string | null;
  attempt_id: string | null;
  task_id: string;
  workspace_id: string;
  title: string;
  repo_url: string;
  base_branch: string;
  branch_name: string | null;
  pr_url: string;
  status: WorkspaceStatus;
  auto_merge: boolean;
  task_class: string | null;
  owned_paths: string[];
  created_at: string;
  updated_at: string;
  last_event: WorkspaceEvent | null;
  merge_blocker_reason: MergeBlockerReason;
  readiness: MergeCandidateReadiness | null;
  canonical: boolean;
  latest_validation: ValidationRunSummary | null;
  stale_reasons: StaleReason[];
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

export interface WorkspaceRetryResponse {
  source_workspace_id: string;
  new_workspace_id: string;
  operation_id: string;
  status: WorkspaceStatus;
  attempt_number: number;
  status_url: string;
  events_url: string;
}

export interface ListEnvelope<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface WorkspaceSaturationCounts {
  by_status: Record<string, number>;
  active_total: number;
  requested: number;
  provisioning: number;
  ready: number;
  running: number;
  validating: number;
  pushing: number;
  monitoring_pr: number;
  destroying: number;
  completed: number;
  failed: number;
  cancelled: number;
  destroyed: number;
}

export interface WorkerConcurrencySettings {
  max_concurrent_provisions: number;
  max_concurrent_executions: number;
}

export interface WorkspaceResourceDefaults {
  steady_cpu: number;
  steady_memory_gb: number;
  peak_cpu: number;
  peak_memory_gb: number;
}

export interface ReservedResources {
  active_workspace_count: number;
  steady_cpu: number;
  steady_memory_gb: number;
  peak_cpu: number;
  peak_memory_gb: number;
}

export interface ConcurrencyLane {
  limit: number;
  in_use: number;
  queued: number;
  available: number;
}

export interface ResourceConcurrency {
  provision: ConcurrencyLane;
  execution: ConcurrencyLane;
}

export interface DiskCheck {
  path: string;
  checked_path: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent_free: number;
  threshold_bytes: number;
  ok: boolean;
  status: string;
  reason: string;
  detail: string | null;
}

export interface AdmissionSummary {
  ok: boolean;
  status: string;
  reason: string;
  detail: string | null;
}

export interface ResourceSaturationSummary {
  generated_at: string;
  workspace_counts: WorkspaceSaturationCounts;
  worker: WorkerConcurrencySettings;
  resource_defaults: WorkspaceResourceDefaults;
  reserved_resources: ReservedResources;
  concurrency: ResourceConcurrency;
  disk: DiskCheck;
  admission: AdmissionSummary;
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

export interface FailureExample {
  workspace_id: string;
  title: string;
  repo_url: string;
  agent: string;
  timestamp: string;
  failure_reason: string;
  failure_message: string;
  pr_url: string | null;
}

export interface FailureTaxonomyCount {
  reason: string;
  count: number;
}

export interface FailureSummaryResponse {
  total_failures: number;
  window_hours: number;
  taxonomy: FailureTaxonomyCount[];
  latest_examples: FailureExample[];
}
