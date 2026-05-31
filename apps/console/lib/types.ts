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

export type AgentRuntime = "codex" | "claude_code" | "gemini" | "opencode";

export type AgentIdentitySource = "task_policy" | "default" | "unavailable";
export type NetworkPosture = "offline" | "restricted" | "open";

export type WorkspaceLifecycleStageStatus =
  | "pending"
  | "active"
  | "completed"
  | "terminal_skipped";

export interface WorkspaceLifecycleStage {
  stage: string;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  status: WorkspaceLifecycleStageStatus;
}

export interface LlmUsageSummary {
  input_tokens: number | null;
  cached_input_tokens?: number | null;
  output_tokens: number | null;
  reasoning_output_tokens?: number | null;
  total_tokens: number | null;
  cost_estimate: number | null;
  currency: string | null;
  status: "available" | "unavailable";
  source: string;
  reason: string | null;
}

export interface PricingMetadata {
  provider: string;
  model: string;
  currency: string;
  unit: string;
  price_per_unit: number | null;
  timestamp: string;
  version: number | null;
  is_current: boolean;
}

export interface WorkspaceRecoveryCurrentOperation {
  id: string;
  type: string;
  status: string;
  created_at: string;
  started_at: string | null;
  payload: Record<string, unknown> | null;
}

export interface WorkspaceRecoverySummary {
  from_state: string | null;
  to_state: string | null;
  reason_code: string | null;
  action: string | null;
  recovery_mode: string | null;
  started_at: string;
  current_operation: WorkspaceRecoveryCurrentOperation | null;
  summary: string;
  payload: Record<string, unknown> | null;
}

export interface QueueDecisionSummary {
  id: string;
  decision: string;
  reason_code: string;
  class_priority: number;
  computed_priority: number;
  age_boost: number;
  retry_bonus: number;
  resource_summary: Record<string, unknown>;
  overlap_risk_summary: Record<string, unknown>;
  decided_at: string;
}

export interface ResourceReservationSummary {
  id: string;
  node_id: string;
  steady_cpu: number;
  steady_memory_gb: number;
  peak_cpu: number;
  peak_memory_gb: number;
  disk_mb: number | null;
  dind_slots: number;
  phase: string;
  reserved_at: string;
  released_at: string | null;
}

export interface CoordinationOverlap {
  workspace_id: string;
  existing_path: string;
  requested_path: string;
  match_reason_code?: string | null;
  explanation?: string | null;
}

export interface WorkspaceCoordinationWarning {
  warning_code: string;
  message: string;
  severity: "advisory";
  blocks_launch: boolean;
  workspace_ids: string[];
  overlaps: CoordinationOverlap[];
  stale_policy_context: {
    trigger_type?: string;
    stale_reason_code?: string;
  };
  overlap_count: number;
  overlaps_truncated: boolean;
}

export interface ProviderReadinessCredentialSource {
  type: string | null;
  signal: string | null;
  credential_scope: string | null;
  isolation: string | null;
}

export interface ProviderReadinessPreflight {
  provider: string;
  agent: string;
  model: string | null;
  model_source: string | null;
  readiness_status: string;
  auth_status: string;
  auth_source: string;
  credential_scope: string | null;
  isolation: string | null;
  probe_status: string;
  reason_code: string;
  message: string;
  override_required: boolean;
  override_requested: boolean;
  override_used: boolean;
  override_reason: string | null;
  blocks_launch: boolean;
  checked_at: string;
  credential_sources: ProviderReadinessCredentialSource[];
  warnings?: Record<string, unknown>[];
  probe_detail?: string | null;
  source_workspace_id?: string | null;
}

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
  task_prompt: string;
  repo_url: string;
  base_branch: string;
  branch_name: string | null;
  agent: AgentRuntime;
  agent_model: string | null;
  agent_effort: string | null;
  agent_model_source: AgentIdentitySource;
  agent_effort_source: AgentIdentitySource;
  network_posture: NetworkPosture | null;
  lifecycle: WorkspaceLifecycleStage[];
  llm_usage: LlmUsageSummary;
  pricing?: PricingMetadata | null;
  recovery?: WorkspaceRecoverySummary | null;
  coordination_warnings: WorkspaceCoordinationWarning[];
  provider_readiness_preflight?: ProviderReadinessPreflight | null;
  status: WorkspaceStatus;

  subphase: string | null;
  last_activity_at: string | null;
  last_log_at: string | null;
  is_stale_running: boolean;
  current_phase: string;
  active_operation: string | null;
  last_event: WorkspaceEvent | null;
  pr_url: string | null;
  failure_reason: string | null;
  failure_message: string | null;
  latest_queue_decision?: QueueDecisionSummary | null;
  active_resource_reservation?: ResourceReservationSummary | null;
  created_at: string;
  updated_at: string;
}

export type MergeBlockerReason =
  | "ready_to_merge_or_waiting_for_github"
  | "manual_merge_required"
  | "waiting_for_monitor"
  | "waiting_for_older_candidate"
  | "workspace_not_terminal"
  | "completed"
  | "failed_or_cancelled"
  | "not_canonical"
  | "policy_blocked"
  | "stale";

export type MergeCandidateStatus = "open" | "merged" | "closed";

export type MergeQueueBlockerState = "merge_eligible" | "monitor_owned_recovery";

export interface MergeCandidateReadiness {
  ready: boolean;
  manual_merge_required: boolean;
  waiting_for_monitor: boolean;
  failed_or_cancelled: boolean;
  completed: boolean;
  not_canonical: boolean;
  stale: boolean;
  stale_reason: string | null;
}

export type ValidationTier = 1 | 2 | 3;

export type ValidationProvenanceStatus = "running" | "succeeded" | "failed" | "unknown";
export type ValidationFreshnessStatus = "fresh" | "stale" | "unknown" | "unavailable";
export type ValidationIdentitySource = "persisted" | "legacy_fallback";

export interface ValidationRunSummary {
  validation_run_id: string;
  attempt_id: string | null;
  tier: ValidationTier;
  command_set_hash: string;
  base_commit: string | null;
  base_sha: string | null;
  workspace_head_sha: string | null;
  target_branch: string | null;
  target_head_sha: string | null;
  current_target_head_sha: string | null;
  profile_name: string | null;
  profile_version: number | null;
  profile_source: string | null;
  resolved_profile_digest: string | null;
  environment_identity_digest: string | null;
  environment_identity_inputs: Record<string, unknown>;
  identity_source: ValidationIdentitySource;
  status: ValidationProvenanceStatus;
  reason_code: string | null;
  started_at: string;
  finished_at: string | null;
  log_stream_refs: Record<string, unknown>;
  fresh_for_target: boolean | null;
  freshness_status: ValidationFreshnessStatus;
  freshness_reason_code: string | null;
  retry_count: number;
  coverage_percent: number | null;
  coverage_minimum_percent: number | null;
  coverage_status: string | null;
  coverage_reason_code: string | null;
  coverage_gaps: Record<string, unknown>[];
}

export interface ValidationFreshnessSummary {
  required_tier: ValidationTier | null;
  latest_satisfied_tier: ValidationTier | null;
  freshness_status: ValidationFreshnessStatus;
  reason_code: string | null;
  current_target_head_sha: string | null;
  latest_validation: ValidationRunSummary | null;
}

export interface WorkspaceAppEndpointHealth {
  path: string;
  method: "GET" | "HEAD";
  expected_status: number;
  internal_url: string;
}

export interface WorkspaceAppEndpoint {
  name: string;
  service: string;
  scheme: "http" | "https";
  port: number;
  path: string;
  internal_url: string;
  visibility: "agent" | "validation" | "console";
  health: WorkspaceAppEndpointHealth | null;
}

export type StaleReasonCode =
  | "STALE_TARGET_ADVANCED"
  | "STALE_OVERLAP"
  | "STALE_DEPENDENCY"
  | "STALE_BUILD_CONFIG"
  | "STALE_SCHEMA"
  | "ADVISORY_PLAN_ARTIFACT_OVERLAP";

export type StaleReasonTrigger =
  | "target_advanced"
  | "path_overlap"
  | "schema_changed"
  | "dependency_changed"
  | "build_config_changed"
  | "plan_artifact_overlap";

export type StaleReasonStatus = "active" | "resolved";
export type StaleReasonSeverity = "blocking" | "advisory";

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
  severity: StaleReasonSeverity;
  blocks_merge: boolean;
  detected_at: string;
  resolved_at: string | null;
}

export type PolicyFindingSeverity = "warning" | "blocking";
export type PolicyFindingStatus = "active" | "resolved";
export type PolicyFindingReasonCode = "OUT_OF_SCOPE_CHANGE";

export interface PolicyFinding {
  id: string;
  workspace_id: string;
  candidate_id: string | null;
  attempt_id: string | null;
  task_id: string | null;
  reason_code: PolicyFindingReasonCode;
  severity: PolicyFindingSeverity;
  subject_path: string | null;
  explanation: string;
  details: Record<string, unknown>;
  status: PolicyFindingStatus;
  detected_at: string;
  resolved_at: string | null;
}

export interface MergeQueueBlocker {
  candidate_id: string;
  workspace_id: string;
  attempt_id: string;
  task_id: string;
  title: string;
  pr_url: string;
  pr_number: number | null;
  status: WorkspaceStatus;
  blocker_state: MergeQueueBlockerState;
  reason_code: string;
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
  merged_at: string | null;
  last_event: WorkspaceEvent | null;
  merge_blocker_reason: MergeBlockerReason;
  required_next_action: string | null;
  required_validation_tier?: ValidationTier | null;
  latest_satisfied_validation_tier?: ValidationTier | null;
  validation_freshness_status?: ValidationFreshnessStatus;
  validation_reason_code?: string | null;
  readiness: MergeCandidateReadiness | null;
  canonical: boolean;
  queue_blockers: MergeQueueBlocker[];
  latest_validation: ValidationRunSummary | null;
  stale_reasons: StaleReason[];
  policy_findings: PolicyFinding[];
}

export interface WorkspaceSecretLease {
  lease_id: string;
  secret_name: string;
  kind: "mount" | "env";
  target: string;
  status: "issued" | "mounted" | "expired" | "revoked";
  provider: string | null;
  ref_digest: string | null;
  issued_at: string;
  mounted_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface WorkspaceEgressAudit {
  id: string;
  workspace_id: string;
  attempt_id: string | null;
  policy_posture: string;
  decision: string;
  destination_category: string;
  reason_code: string;
  details: Record<string, unknown>;
  enforced_at: string;
  created_at: string;
}

export interface ProfileSecret {
  name: string;
  target: string;
  kind: "mount" | "env";
  mode: "ro" | "rw";
  required: boolean;
  provider: string | null;
}

export interface ProfileEgress {
  mode: NetworkPosture | "unavailable";
}

export interface HostHomeAuthMountPolicy {
  mode: "block" | "warn" | "unavailable";
}

export interface ProfileSecurity {
  egress: ProfileEgress;
  host_home_auth_mounts: HostHomeAuthMountPolicy;
}

export interface Workspace {
  id: string;
  status: WorkspaceStatus;

  subphase: string | null;
  last_activity_at: string | null;
  last_log_at: string | null;
  is_stale_running: boolean;
  version: number;
  repo_url: string;
  branch_base: string;
  branch_name: string | null;
  base_commit: string | null;
  task_title: string;
  task_prompt: string;
  task_external_id: string | null;
  task_class: string | null;
  owned_paths: string[];
  task_policy: Record<string, unknown>;
  auto_merge: boolean;
  initial_review_grace_period_seconds: number | null;
  agent: AgentRuntime;
  agent_model: string | null;
  agent_effort: string | null;
  agent_model_source: AgentIdentitySource;
  agent_effort_source: AgentIdentitySource;
  lifecycle: WorkspaceLifecycleStage[];
  llm_usage: LlmUsageSummary;
  pricing?: PricingMetadata | null;
  recovery?: WorkspaceRecoverySummary | null;
  coordination_warnings: WorkspaceCoordinationWarning[];
  provider_readiness_preflight: ProviderReadinessPreflight | null;
  validation_provenance?: ValidationFreshnessSummary | null;
  app_endpoints: WorkspaceAppEndpoint[];
  env_profile: string | null;
  profile_ref: string | null;
  requested_profile: Record<string, unknown> | null;
  resolved_profile: Record<string, unknown> | null;
  network_posture: NetworkPosture | null;
  test_commands: string[];
  requires_database: boolean;
  node_id: string | null;
  compose_project_name: string | null;
  compose_file_path: string | null;
  pr_url: string | null;
  failure_reason: string | null;
  failure_message: string | null;
  secret_leases: WorkspaceSecretLease[];
  policy_findings: PolicyFinding[];
  egress_audit: WorkspaceEgressAudit | null;
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
  app_endpoints: WorkspaceAppEndpoint[];
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
  owner: string | null;
  source: string | null;
  action: string | null;
  pr_number: number | null;
  pr_url: string | null;
  source_head_sha: string | null;
  source_base_sha: string | null;
  reason: string | null;
  reason_code: string | null;
  failure_code: string | null;
  failure_message: string | null;
  log_stream_refs: Record<string, unknown>;
  log_stream_ids: string[];
}

export type WorkspaceOperatorAction = "remonitor" | "refresh" | "revalidate";

export interface WorkspaceControlWarning {
  warning_code: string;
  message: string;
}

export interface WorkspaceControlResponse {
  workspace_id: string;
  operation_id: string;
  operation_status: string;
  status: WorkspaceStatus;
  message: string;
  warnings: WorkspaceControlWarning[];
}

export interface WorkspaceOperatorRequest {
  reason?: string;
  workspace_version?: number;
  requested_tier?: ValidationTier;
  idempotency_key?: string;
}

export interface WorkspaceRetryResponse {
  source_workspace_id: string;
  new_workspace_id: string;
  operation_id: string;
  status: WorkspaceStatus;
  attempt_number: number;
  status_url: string;
  events_url: string;
  provider_readiness_preflight?: ProviderReadinessPreflight | null;
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

export type LocalCapacitySourceValue = "docker" | "operator_config" | "mixed";

export interface LocalCapacitySource {
  cpu_cores: number | null;
  memory_gb: number | null;
  source: string | null;
  reason_code: string | null;
  detail: string | null;
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
  disk_mb: number;
  dind_slots: number;
}

export interface QueuePlannedResources {
  steady_cpu: number;
  steady_memory_gb: number;
  peak_cpu: number;
  peak_memory_gb: number;
  disk_mb: number;
  dind_slots: number;
}

export interface CapacityDimension {
  limit: number | null;
  reserved: number;
  available: number | null;
  available_after_next_default: number | null;
  reason_code: string | null;
}

export interface ResourceCapacitySummary {
  steady_cpu: CapacityDimension;
  peak_cpu: CapacityDimension;
  steady_memory_gb: CapacityDimension;
  peak_memory_gb: CapacityDimension;
  disk_mb: CapacityDimension;
  dind_slots: CapacityDimension;
  pressure_reasons: string[];
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

export interface CapacityQueueSummary {
  queued_workspace_count: number;
  oldest_workspace_id: string | null;
  oldest_wait_seconds: number | null;
  planned_resources: QueuePlannedResources;
  blocked_reason_counts: Record<string, number>;
}

export interface ResourceSaturationSummary {
  generated_at: string;
  workspace_counts: WorkspaceSaturationCounts;
  worker: WorkerConcurrencySettings;
  local_capacity: LocalCapacitySource;
  resource_defaults: WorkspaceResourceDefaults;
  reserved_resources: ReservedResources;
  capacity: ResourceCapacitySummary;
  allocated_resources: ReservedResources;
  allocated_capacity: ResourceCapacitySummary;
  capacity_queue: CapacityQueueSummary;
  concurrency: ResourceConcurrency;
  disk: DiskCheck;
  admission: AdmissionSummary;
}

export type AwfStreamFrame =
  | { type: "connected"; workspace_id: string }
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
  | { type: "closed"; workspace_id: string; code: number; reason: string }
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

export interface WorkspaceReliabilitySummary {
  generated_at: string;
  window_start: string;
  since_hours: number;
  status_counts: Record<string, number>;
  failure_reason_counts: Record<string, number>;
  active_count: number;
  destroying_count: number;
  completed_count: number;
  failed_count: number;
  cancelled_count: number;
  destroyed_count: number;
  cleanup_failure_count: number;
  stuck_count: number;
  actionable_reason_count: number;
  unactionable_reason_count: number;
}
