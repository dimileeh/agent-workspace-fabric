import type {
  CapacityQueueSummary,
  HostHomeAuthMountPolicy,
  Operation,
  PolicyFinding,
  ProfileEgress,
  ProfileSecret,
  ProfileSecurity,
  ValidationFreshnessSummary,
  ValidationFreshnessStatus,
  ValidationRunSummary,
  Workspace,
  WorkspaceAppEndpoint,
  WorkspaceAppEndpointHealth,
  WorkspaceCoordinationWarning,
  WorkspaceControlResponse,
  WorkspaceControlWarning,
  WorkspaceEgressAudit,
  WorkspaceOperatorAction,
  WorkspaceOperatorRequest,
  WorkspaceOverview,
  WorkspaceRecoverySummary,
  WorkspaceRuntime,
  WorkspaceSecretLease,
} from "./types";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends <Value>() => Value extends Right ? 1 : 2
    ? (<Value>() => Value extends Right ? 1 : 2) extends <Value>() => Value extends Left ? 1 : 2
      ? true
      : false
    : false;

type Expect<Condition extends true> = Condition;

type WorkspaceRecoverySummaryTextIsRequired = Expect<
  Equal<WorkspaceRecoverySummary["summary"], string>
>;

export const workspaceRecoverySummaryTextIsRequired: WorkspaceRecoverySummaryTextIsRequired = true;

type WorkspaceOverviewIncludesTaskPrompt = Expect<
  Equal<Pick<WorkspaceOverview, "task_prompt">, { task_prompt: string }>
>;

export const workspaceOverviewIncludesTaskPrompt: WorkspaceOverviewIncludesTaskPrompt = true;

type CapacityQueuePlannedResourcesOmitActiveCount = Expect<
  Equal<
    CapacityQueueSummary["planned_resources"],
    {
      steady_cpu: number;
      steady_memory_gb: number;
      peak_cpu: number;
      peak_memory_gb: number;
      disk_mb: number;
      dind_slots: number;
    }
  >
>;

export const capacityQueuePlannedResourcesOmitActiveCount: CapacityQueuePlannedResourcesOmitActiveCount = true;

type CoordinationWarningShape = Expect<
  Equal<
    Pick<
      WorkspaceCoordinationWarning,
      "warning_code" | "severity" | "blocks_launch" | "workspace_ids" | "stale_policy_context"
    >,
    {
      warning_code: string;
      severity: "advisory";
      blocks_launch: boolean;
      workspace_ids: string[];
      stale_policy_context: {
        trigger_type?: string;
        stale_reason_code?: string;
      };
    }
  >
>;

type WorkspaceSurfacesIncludeCoordinationWarnings = Expect<
  Equal<
    Pick<Workspace, "coordination_warnings">,
    { coordination_warnings: WorkspaceCoordinationWarning[] }
  >
>;

type WorkspaceOverviewIncludesCoordinationWarnings = Expect<
  Equal<
    Pick<WorkspaceOverview, "coordination_warnings">,
    { coordination_warnings: WorkspaceCoordinationWarning[] }
  >
>;

export const coordinationWarningShape: CoordinationWarningShape = true;
export const workspaceSurfacesIncludeCoordinationWarnings: WorkspaceSurfacesIncludeCoordinationWarnings = true;
export const workspaceOverviewIncludesCoordinationWarnings: WorkspaceOverviewIncludesCoordinationWarnings = true;

type WorkspaceValidationProvenanceAllowsMissing = Expect<
  Equal<
    Pick<Workspace, "validation_provenance">,
    { validation_provenance?: ValidationFreshnessSummary | null }
  >
>;

type ValidationRunSummaryIdentityFieldsArePresent = Expect<
  Equal<
    Pick<
      ValidationRunSummary,
      | "base_sha"
      | "workspace_head_sha"
      | "profile_name"
      | "profile_version"
      | "profile_source"
      | "resolved_profile_digest"
      | "environment_identity_digest"
      | "environment_identity_inputs"
      | "identity_source"
      | "freshness_status"
      | "freshness_reason_code"
    >,
    {
      base_sha: string | null;
      workspace_head_sha: string | null;
      profile_name: string | null;
      profile_version: number | null;
      profile_source: string | null;
      resolved_profile_digest: string | null;
      environment_identity_digest: string | null;
      environment_identity_inputs: Record<string, unknown>;
      identity_source: "persisted" | "legacy_fallback";
      freshness_status: ValidationFreshnessStatus;
      freshness_reason_code: string | null;
    }
  >
>;

export const workspaceValidationProvenanceAllowsMissing: WorkspaceValidationProvenanceAllowsMissing = true;
export const validationRunSummaryIdentityFieldsArePresent: ValidationRunSummaryIdentityFieldsArePresent = true;

type WorkspaceAppEndpointHealthShape = Expect<
  Equal<
    WorkspaceAppEndpointHealth,
    {
      path: string;
      method: "GET" | "HEAD";
      expected_status: number;
      internal_url: string;
    }
  >
>;

type WorkspaceAppEndpointShape = Expect<
  Equal<
    WorkspaceAppEndpoint,
    {
      name: string;
      service: string;
      scheme: "http" | "https";
      port: number;
      path: string;
      internal_url: string;
      visibility: "agent" | "validation" | "console";
      health: WorkspaceAppEndpointHealth | null;
    }
  >
>;

type WorkspaceDetailIncludesAppEndpoints = Expect<
  Equal<Pick<Workspace, "app_endpoints">, { app_endpoints: WorkspaceAppEndpoint[] }>
>;

type WorkspaceRuntimeIncludesAppEndpoints = Expect<
  Equal<Pick<WorkspaceRuntime, "app_endpoints">, { app_endpoints: WorkspaceAppEndpoint[] }>
>;

export const workspaceAppEndpointHealthShape: WorkspaceAppEndpointHealthShape = true;
export const workspaceAppEndpointShape: WorkspaceAppEndpointShape = true;
export const workspaceDetailIncludesAppEndpoints: WorkspaceDetailIncludesAppEndpoints = true;
export const workspaceRuntimeIncludesAppEndpoints: WorkspaceRuntimeIncludesAppEndpoints = true;

type OperationAuditFieldsAreNullable = Expect<
  Equal<
    Pick<
      Operation,
      | "owner"
      | "source"
      | "action"
      | "pr_number"
      | "pr_url"
      | "source_head_sha"
      | "source_base_sha"
      | "reason"
      | "reason_code"
      | "failure_code"
      | "failure_message"
      | "log_stream_refs"
      | "log_stream_ids"
    >,
    {
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
  >
>;

export const operationAuditFieldsAreNullable: OperationAuditFieldsAreNullable = true;

type WorkspaceOperatorActionUnionIsExplicit = Expect<
  Equal<WorkspaceOperatorAction, "remonitor" | "refresh" | "revalidate">
>;

type WorkspaceControlResponseShape = Expect<
  Equal<
    Pick<
      WorkspaceControlResponse,
      "workspace_id" | "operation_id" | "operation_status" | "status" | "message" | "warnings"
    >,
    {
      workspace_id: string;
      operation_id: string;
      operation_status: string;
      status: Workspace["status"];
      message: string;
      warnings: WorkspaceControlWarning[];
    }
  >
>;

type WorkspaceControlWarningShape = Expect<
  Equal<
    WorkspaceControlWarning,
    {
      warning_code: string;
      message: string;
    }
  >
>;

type WorkspaceOperatorRequestShape = Expect<
  Equal<
    WorkspaceOperatorRequest,
    {
      reason?: string;
      workspace_version?: number;
      requested_tier?: 1 | 2 | 3;
      idempotency_key?: string;
    }
  >
>;

export const workspaceOperatorActionUnionIsExplicit: WorkspaceOperatorActionUnionIsExplicit = true;
export const workspaceControlResponseShape: WorkspaceControlResponseShape = true;
export const workspaceControlWarningShape: WorkspaceControlWarningShape = true;
export const workspaceOperatorRequestShape: WorkspaceOperatorRequestShape = true;

type WorkspaceSecretLeaseShape = Expect<
  Equal<
    WorkspaceSecretLease,
    {
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
  >
>;

type ProfileSecretShape = Expect<
  Equal<
    ProfileSecret,
    {
      name: string;
      target: string;
      kind: "mount" | "env";
      mode: "ro" | "rw";
      required: boolean;
      provider: string | null;
    }
  >
>;

type ProfileEgressShape = Expect<
  Equal<
    ProfileEgress,
    {
      mode: "open" | "restricted" | "offline" | "unavailable";
    }
  >
>;

type HostHomeAuthMountPolicyShape = Expect<
  Equal<
    HostHomeAuthMountPolicy,
    {
      mode: "block" | "warn" | "unavailable";
    }
  >
>;

type ProfileSecurityShape = Expect<
  Equal<
    ProfileSecurity,
    {
      egress: ProfileEgress;
      host_home_auth_mounts: HostHomeAuthMountPolicy;
    }
  >
>;

type WorkspaceIncludesSecretLeases = Expect<
  Equal<
    Pick<Workspace, "secret_leases">,
    { secret_leases: WorkspaceSecretLease[] }
  >
>;

type WorkspaceIncludesPolicyFindings = Expect<
  Equal<
    Pick<Workspace, "policy_findings">,
    { policy_findings: PolicyFinding[] }
  >
>;

type WorkspaceEgressAuditShape = Expect<
  Equal<
    WorkspaceEgressAudit,
    {
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
  >
>;

type WorkspaceIncludesEgressAudit = Expect<
  Equal<
    Pick<Workspace, "egress_audit">,
    { egress_audit: WorkspaceEgressAudit | null }
  >
>;

export const workspaceSecretLeaseShape: WorkspaceSecretLeaseShape = true;
export const profileSecretShape: ProfileSecretShape = true;
export const profileEgressShape: ProfileEgressShape = true;
export const hostHomeAuthMountPolicyShape: HostHomeAuthMountPolicyShape = true;
export const profileSecurityShape: ProfileSecurityShape = true;
export const workspaceIncludesSecretLeases: WorkspaceIncludesSecretLeases = true;
export const workspaceIncludesPolicyFindings: WorkspaceIncludesPolicyFindings = true;
export const workspaceEgressAuditShape: WorkspaceEgressAuditShape = true;
export const workspaceIncludesEgressAudit: WorkspaceIncludesEgressAudit = true;
