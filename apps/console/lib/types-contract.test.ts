import type {
  Operation,
  ValidationFreshnessSummary,
  ValidationFreshnessStatus,
  ValidationRunSummary,
  Workspace,
  WorkspaceRecoverySummary,
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
